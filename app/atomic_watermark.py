"""P1b B1 — Redis success watermark: schema + serialization + pure relation classifier (§19 B1, dormant).

PR_D_RECOVERY_SPEC.md §12 success-watermark(D)의 **데이터 구조 계층**. pure — Redis I/O·arbitration·
seq 발급·bootstrap 결정·eviction-alive 연속성은 **B2b coordinator 소유**(in-memory state + control
plane §15/§18). B1이 제공하는 것:
- `Watermark` dataclass: asset별 success revision vector identity (§12 line 57/222).
- serialize/deserialize: fail-closed (version/타입/중복/교집합 오염 → WatermarkSchemaError → 호출자 bootstrap).
- `watermark_content_equal`: §12 line 219 "실제 전송한 vector 전체를 하나의 identity로 비교(component
  merge 금지)" — B3 "identical partial 재발행 금지"(§19 line 349) + sent_but_uncommitted idempotency
  (§12 line 218) 판정에 B2b가 사용.
- `classify_watermark_relation`: 순수 분류기 (write/skip/bootstrap/alert action 매핑은 B2b).
- `is_watermark_compatible`: membership/schema mismatch → bootstrap trigger predicate (§19 line 339;
  결정은 B2b).
- `watermark_key`: FX asset 전용 Redis key 문자열 helper.

**B1이 안 하는 것 (B2b coordinator 소유)**:
- lineage arbitration — `LINEAGE_MISMATCH`만 반환. bootstrap-wins 판정은 B2b가 durable control plane
  active session/generation + lease owner(§15 line 194 / §18 line 314) 대조로 — stale bootstrap retry /
  lease takeover 뒤 old lineage는 fail-closed/alert (단순 bootstrap-wins 아님).
- publish_sequence 발급·monotonicity — coordinator in-memory 소유. eviction-alive seq 연속성(§19 line
  338)을 watermark-derived로 하면 regression 위험이라 `next_sequence`/`is_completion`은 B1 미포함.
- completion gate (sent_count>0 후에만 watermark write, §12 line 217) — B2b 흐름.
- lineage ordering by wall-clock epoch — 시계 역행 취약이라 미사용. lineage_id는 **opaque** 식별자
  (B2b가 session_id:generation 등으로 조합), B1은 동일성(==)만 본다. epoch 비교축 없음.

**dormant**: live writer/coordinator가 본 모듈을 import/호출하지 않음 (B2b wiring·C6 활성화 후속).
재사용: `atomic_revision.Revision` / `atomic_value_schema.{make_revision_key_from_revision, parse_revision_key}`
(vector 인코딩 single-source). → atomic_watermark는 dormant island 합류 (atomic_value_schema import).
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict, Optional, Tuple

from app.atomic_value_schema import parse_revision_key

# watermark 직렬화 schema discriminator. 불일치 = 비호환 → deserialize fail-closed → 호출자(B2b) bootstrap.
WATERMARK_SCHEMA_VERSION = 1


class WatermarkSchemaError(ValueError):
    """deserialize fail-closed — version mismatch / 필드 누락 / 타입·중복·교집합 오염.

    §12 line 222 "유실·비호환 시에만 bootstrap" — 호출자(B2b)는 이 예외를 'incompatible → 새 lineage
    bootstrap' directive로 해석한다 (B1은 '비호환'을 결정적으로 신호만 하고 bootstrap 자체는 B2b 소유)."""


class WatermarkRelation(enum.Enum):
    """current ↔ incoming watermark 관계 분류 (순수). action 매핑(write/skip/bootstrap/alert)은 B2b.

    plain Enum (str mixin 아님) — A4 WriteState 패턴 동일, 우연한 문자열 비교 충돌 회피.
    """
    NO_CURRENT = "no_current"                          # current 부재 — B2b가 first-write/bootstrap 결정
    LINEAGE_MISMATCH = "lineage_mismatch"              # lineage 다름 — B2b가 control plane 대조로 판정
    NEWER = "newer"                                    # same lineage, incoming.seq > current
    OLDER = "older"                                    # same lineage, incoming.seq < current (skip)
    SAME = "same"                                      # same lineage, seq== AND content 동일 (idempotent)
    SAME_SEQ_CONTENT_DIVERGENT = "same_seq_content_divergent"  # same seq, content 다름 = coordinator invariant 위반 (B2b alert)


@dataclass(frozen=True)
class Watermark:
    """asset별 success watermark identity (§12 line 57/222).

    - asset: FX asset (usd-krw/jpy-krw/eur-krw). per-asset watermark.
    - lineage_id: **opaque** lineage 식별자 (B2b 발급, 예: session_id:generation). B1은 동일성만 비교.
    - publish_sequence: coordinator 발급 per-asset lineage 내 monotonic int (B1은 받기만, 발급 안 함).
    - membership_version: source-set membership 버전 (mismatch → bootstrap trigger, 결정은 B2b).
    - present_revision_vector: {source: revision_key(make_revision_key 형식)} — **effective-only**
      (None 성분 금지; skipped_newer effective 확보는 B2b §15 line 215). §12 "vector 전체 = identity".
    - missing_sources: 정렬·중복없는 source 튜플 (telemetry/reconciliation input). present와 disjoint.
    - sent_at: telemetry용 ISO 문자열 (content identity 아님).

    frozen — binding 불변(dict/tuple 내용은 deep-freeze 아니나 생성 후 변경 안 함). __eq__는 전 필드
    content 비교(round-trip 동등성 테스트용). hashable 아님(dict 필드) — 해시 불필요.
    """
    asset: str
    lineage_id: str
    publish_sequence: int
    membership_version: int
    present_revision_vector: Dict[str, str]
    missing_sources: Tuple[str, ...]
    sent_at: str

    def __post_init__(self):
        """단일 invariant 강제점 — 직접 생성 + deserialize 공통 (codex cross-check P1).

        직접 `Watermark(...)`가 invariant를 우회하면 watermark_content_equal이 비정렬 missing_sources를
        다른 content로 오판(same-seq idempotency → SAME_SEQ_CONTENT_DIVERGENT). __post_init__이 validate
        + canonicalize(정렬 tuple)해 직접 생성·deserialize 양쪽이 동일 보장. frozen이라 object.__setattr__.
        """
        for fname, v in (("asset", self.asset), ("lineage_id", self.lineage_id), ("sent_at", self.sent_at)):
            if not isinstance(v, str) or not v:
                raise ValueError(f"Watermark.{fname}: non-empty str — got {v!r}")
        for fname, v in (("publish_sequence", self.publish_sequence),
                         ("membership_version", self.membership_version)):
            # bool(int 서브클래스)·float(int 인스턴스 아님)·음수 모두 차단
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"Watermark.{fname}: non-negative int — got {v!r}")
        if not isinstance(self.present_revision_vector, dict):
            raise ValueError(f"Watermark.present_revision_vector: dict — got {self.present_revision_vector!r}")
        for src, val in self.present_revision_vector.items():
            if not isinstance(src, str) or not src:
                raise ValueError(f"Watermark.present_revision_vector: source 키 non-empty str — got {src!r}")
            if not isinstance(val, str):  # None 성분 금지(effective-only) + 타입 오염
                raise ValueError(f"Watermark.present_revision_vector[{src!r}]: str revision_key — got {val!r}")
            parse_revision_key(val)  # revision_key 형식 검증 (오염 → ValueError)
        if not isinstance(self.missing_sources, (list, tuple)):
            raise ValueError(f"Watermark.missing_sources: list|tuple — got {self.missing_sources!r}")
        seen = set()
        for s in self.missing_sources:
            if not isinstance(s, str) or not s:
                raise ValueError(f"Watermark.missing_sources: 원소 non-empty str — got {s!r}")
            if s in seen:
                raise ValueError(f"Watermark.missing_sources 중복 — {s!r}")
            seen.add(s)
        overlap = seen & set(self.present_revision_vector.keys())
        if overlap:
            raise ValueError(f"Watermark: source는 present/missing 동시 불가(교집합) — {sorted(overlap)}")
        # canonicalize — 정렬 tuple (content_equal 직접 비교 안정; 직접 생성도 정규화)
        object.__setattr__(self, "missing_sources", tuple(sorted(seen)))
        # vector defensive copy + read-only — frozen이 dict를 deep-freeze 못 하므로 caller-ref bleed 및
        # 생성 후 변경 차단 (codex caveat). MappingProxyType은 dict와 ==/dict() 호환이라 round-trip 무영향.
        object.__setattr__(self, "present_revision_vector", MappingProxyType(dict(self.present_revision_vector)))


def serialize_watermark(wm: Watermark) -> str:
    """Watermark → JSON 문자열 (serialize_v2_value 패턴). watermark_schema_version 포함."""
    payload = {
        "watermark_schema_version": WATERMARK_SCHEMA_VERSION,
        "asset": wm.asset,
        "lineage_id": wm.lineage_id,
        "publish_sequence": wm.publish_sequence,
        "membership_version": wm.membership_version,
        "present_revision_vector": dict(wm.present_revision_vector),
        "missing_sources": list(wm.missing_sources),
        "sent_at": wm.sent_at,
    }
    return json.dumps(payload, ensure_ascii=False)


def deserialize_watermark(raw) -> Watermark:
    """JSON(str|bytes) → Watermark. fail-closed (WatermarkSchemaError) — 호출자(B2b)가 비호환→bootstrap.

    serialization-level만 직접 검사(bytes 디코드 / JSON object / watermark_schema_version **type-strict**).
    필드/타입/중복/교집합/vector revision_key 형식 invariant는 `Watermark.__post_init__`(단일 강제점)이
    검사하고, 그 ValueError를 WatermarkSchemaError로 정규화한다 (deserialize 계약 = 비호환→bootstrap).
    JSON array(missing_sources) → __post_init__이 정렬 tuple로 canonicalize.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise WatermarkSchemaError(f"deserialize_watermark: bytes 디코드 실패 — {e}") from e
    if not isinstance(raw, str):
        raise WatermarkSchemaError(f"deserialize_watermark: str|bytes만 — got {type(raw).__name__}")
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        raise WatermarkSchemaError(f"deserialize_watermark: JSON 파싱 실패 — {e}") from e
    if not isinstance(obj, dict):
        raise WatermarkSchemaError(f"deserialize_watermark: JSON object 아님 — got {type(obj).__name__}")

    sv = obj.get("watermark_schema_version")
    # type-strict — bool(type is bool)·float(1.0) 모두 차단, discriminator는 정확히 int 1 (codex P2).
    if type(sv) is not int or sv != WATERMARK_SCHEMA_VERSION:
        raise WatermarkSchemaError(
            f"deserialize_watermark: watermark_schema_version != {WATERMARK_SCHEMA_VERSION} (비호환) — got {sv!r}"
        )

    try:
        return Watermark(
            asset=obj.get("asset"),
            lineage_id=obj.get("lineage_id"),
            publish_sequence=obj.get("publish_sequence"),
            membership_version=obj.get("membership_version"),
            present_revision_vector=obj.get("present_revision_vector"),
            missing_sources=obj.get("missing_sources"),
            sent_at=obj.get("sent_at"),
        )
    except ValueError as e:  # __post_init__ invariant 위반 → deserialize 계약상 비호환→bootstrap 신호
        raise WatermarkSchemaError(f"deserialize_watermark: invalid watermark — {e}") from e


def watermark_content_equal(a: Watermark, b: Watermark) -> bool:
    """§12 line 219 vector-whole identity 동등성 — (present_revision_vector + missing_sources +
    membership_version) 일치. lineage_id·publish_sequence·sent_at은 content 아님 (발행 순서/telemetry).

    B3 "identical partial 재발행 금지"(§19 line 349) + sent_but_uncommitted same-content 판정(§12 line
    218)에 B2b가 사용. **asset mismatch → ValueError** (per-asset watermark라 cross-asset 비교는 호출 버그).
    """
    if a.asset != b.asset:
        raise ValueError(f"watermark_content_equal: asset mismatch ({a.asset!r} vs {b.asset!r}) — per-asset 비교만")
    return (
        a.present_revision_vector == b.present_revision_vector
        and a.missing_sources == b.missing_sources
        and a.membership_version == b.membership_version
    )


def classify_watermark_relation(current: Optional[Watermark], incoming: Watermark) -> WatermarkRelation:
    """current ↔ incoming 관계 순수 분류. write/skip/bootstrap/alert action 매핑은 B2b.

    - current None → NO_CURRENT
    - lineage_id 다름 → LINEAGE_MISMATCH (B2b가 control plane active session/gen+lease 대조 — B1 arbitration X)
    - same lineage: seq > → NEWER / seq < → OLDER / seq== AND content 동일 → SAME /
      seq== AND content 다름 → SAME_SEQ_CONTENT_DIVERGENT (coordinator invariant 위반, B2b alert)
    - **asset mismatch → ValueError** (호출 계약 위반)
    """
    if current is None:
        return WatermarkRelation.NO_CURRENT
    if current.asset != incoming.asset:
        raise ValueError(
            f"classify_watermark_relation: asset mismatch ({current.asset!r} vs {incoming.asset!r})"
        )
    if current.lineage_id != incoming.lineage_id:
        return WatermarkRelation.LINEAGE_MISMATCH
    if incoming.publish_sequence > current.publish_sequence:
        return WatermarkRelation.NEWER
    if incoming.publish_sequence < current.publish_sequence:
        return WatermarkRelation.OLDER
    # same lineage + same seq → content로 idempotent vs divergent 판정 (asset 동일 보장)
    if watermark_content_equal(current, incoming):
        return WatermarkRelation.SAME
    return WatermarkRelation.SAME_SEQ_CONTENT_DIVERGENT


def is_watermark_compatible(
    current: Watermark, incoming_membership_version: int, incoming_schema_version: int
) -> bool:
    """membership/schema 호환 predicate (§19 line 339 bootstrap trigger). 결정(bootstrap)은 B2b.

    True = 호환(같은 lineage 이어감) / False = 비호환(B2b가 새 lineage bootstrap 강제).
    schema mismatch는 보통 deserialize에서 먼저 걸리나(부재/corrupt), 정상 schema 안의 membership 값
    변화는 deserialize로 못 잡으므로 별도 predicate 필요. **type-strict** — bool/float incoming은
    비호환(False, 안전쪽 bootstrap)으로 처리 (`True`/`1.0`이 equality로 호환 오판되는 것 차단, codex P2).
    """
    if type(incoming_schema_version) is not int or incoming_schema_version != WATERMARK_SCHEMA_VERSION:
        return False
    if type(incoming_membership_version) is not int:
        return False
    return current.membership_version == incoming_membership_version


def watermark_key(asset: str) -> str:
    """FX asset(usd-krw/jpy-krw/eur-krw) 전용 watermark Redis key 문자열 (순수 helper, I/O 아님).

    non-durable — allkeys-lru eviction 의도적(§19 line 335, 유실→bootstrap). usdt/krx는 topic-only
    (scope 밖). 기존 latest:*/control 네임스페이스와 prefix 비충돌(`watermark:` 전용).
    """
    return f"watermark:fx:{asset}"
