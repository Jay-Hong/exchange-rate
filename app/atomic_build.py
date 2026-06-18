"""P1b B2a — FX BuildResult + dormant builder wrapper (§19 B2a / PR_D §12, dormant).

PR_D line 16: fx builder = `load_and_build_fx_topic_payload` (Redis-first per-source, is_stale 6s DB
fallback) → schema-v1 snapshot. B2a는 그 builder를 dormant wrapper로 감싸 BuildResult를 produce —
payload(실제 발행될 schema-v1) + present/missing/membership/completeness.

**revision vector 미포함 (B2b 전담)**: §12 line 70의 'per-source effective revision vector'는 payload
entry에 revision이 있어야(=v2 Redis 활성) 산출 가능. dormant(v1) 단계엔 effective를 못 만들고, DB
candidate vector는 payload(Redis-first)와 **다른 snapshot**이라 built_revision이 아니다(codex/Workflow
reconcile — Option B). v2 활성 후 builder가 **payload를 만든 같은 Redis read**에서 revision_key를 추출 =
effective vector이고 그건 B2b/C6 wiring. B1 watermark는 effective-only로 이미 잠겨 placeholder vector
금지(atomic_watermark.py). → **B2a는 revision vector를 만들지 않는다.**

**책임 경계**: B2a는 'payload에 실제 present한 source'만 present/missing으로 산출. missing이 no-DB-value냐
fallback-fail이냐 분류 + effective revision 확정 + watermark write = **B2b DERIVED pending(§19:346) 소유**.

**dormant**: live caller 0 (live publisher는 load_and_build_fx_topic_payload를 직접 호출 —
fx_topic_publisher.py:194). dormant=호출 0이지 side-effect-free 아님(loader가 Redis/DB read).
**non-island**: revision 미사용 → atomic_value_schema 미import. live(fx_topic_payload/crud/fx_membership)만 import.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from app.fx_membership import FX_MEMBERSHIP_SOURCES, fx_membership_version
from app.fx_topic_payload import FX_TOPIC_ASSETS, load_and_build_fx_topic_payload


class BuildCompleteness(enum.Enum):
    """build 완전성. action 매핑(block/retry/pending)은 B2b. plain Enum(str mixin 아님, A4 패턴)."""
    COMPLETE = "complete"      # 전 membership source present
    PARTIAL = "partial"        # 일부 missing — **정상 발행 가능**(legacy partial-publish 보존, §19:342)
    MALFORMED = "malformed"    # loader/build 예외 or payload 구조 불량 — 발행 불가(payload None)


def _extract_present_sources(payload: Dict[str, Any]) -> frozenset:
    """schema-v1 payload에서 실제 present한 source 집합 추출 (banks + reference).

    구조 불량(키 부재 등) 시 KeyError/TypeError 전파 → build_fx_result에서 MALFORMED로 흡수."""
    data = payload["data"]
    sources = {entry["source"] for entry in data["banks"]}
    reference = data.get("reference")
    if reference is not None:
        sources.add(reference["source"])
    return frozenset(sources)


@dataclass(frozen=True)
class BuildResult:
    """FX per-asset build 결과 (§19 B2a). revision vector는 미포함 (B2b 전담).

    - COMPLETE/PARTIAL → payload(schema-v1) not None, build_error None.
    - MALFORMED → payload None, build_error non-empty(발행 불가). present/missing 비어있음.
    present/missing = membership 대비 partition(정렬 tuple). present∪missing == FX_MEMBERSHIP_SOURCES.
    __post_init__이 invariant 강제 + present/missing 정렬 canonicalize (직접 생성·factory 공통).
    """
    asset: str
    payload: Optional[Dict[str, Any]]
    present_sources: Tuple[str, ...]
    missing_sources: Tuple[str, ...]
    membership_version: int
    completeness: BuildCompleteness
    build_error: Optional[str]

    def __post_init__(self):
        if not isinstance(self.asset, str) or not self.asset:
            raise ValueError(f"BuildResult.asset: non-empty str — got {self.asset!r}")
        if isinstance(self.membership_version, bool) or not isinstance(self.membership_version, int):
            raise ValueError(f"BuildResult.membership_version: int — got {self.membership_version!r}")

        if self.completeness is BuildCompleteness.MALFORMED:
            if self.payload is not None:
                raise ValueError("BuildResult: MALFORMED는 payload None이어야")
            if not self.build_error:
                raise ValueError("BuildResult: MALFORMED는 build_error non-empty여야")
            if self.present_sources or self.missing_sources:
                raise ValueError("BuildResult: MALFORMED는 present/missing 비어야")
            return

        # COMPLETE / PARTIAL
        if not isinstance(self.payload, dict):
            raise ValueError("BuildResult: COMPLETE/PARTIAL은 payload dict여야")
        if self.build_error is not None:
            raise ValueError("BuildResult: COMPLETE/PARTIAL은 build_error None이어야")
        present = set(self.present_sources)
        missing = set(self.missing_sources)
        if len(present) != len(self.present_sources) or len(missing) != len(self.missing_sources):
            raise ValueError("BuildResult: present/missing 중복")
        if present & missing:
            raise ValueError(f"BuildResult: present/missing 교집합 — {sorted(present & missing)}")
        if not present <= FX_MEMBERSHIP_SOURCES:
            raise ValueError(f"BuildResult: present가 membership 초과 — {sorted(present - FX_MEMBERSHIP_SOURCES)}")
        if present | missing != FX_MEMBERSHIP_SOURCES:
            raise ValueError("BuildResult: present∪missing != FX_MEMBERSHIP_SOURCES (partition 위반)")
        # present_sources ↔ payload 일치 (codex #4 — 중복 필드 drift 방지)
        if _extract_present_sources(self.payload) != present:
            raise ValueError("BuildResult: present_sources가 payload 실제 source와 불일치")
        # completeness ↔ missing 일관 (missing 비면 COMPLETE, 아니면 PARTIAL)
        expected = BuildCompleteness.COMPLETE if not missing else BuildCompleteness.PARTIAL
        if self.completeness is not expected:
            raise ValueError(f"BuildResult: completeness={self.completeness} != {expected}(missing 기준)")
        # canonicalize — 정렬 tuple (content 비교 안정; 직접 생성도 정규화)
        object.__setattr__(self, "present_sources", tuple(sorted(present)))
        object.__setattr__(self, "missing_sources", tuple(sorted(missing)))


def build_fx_result(db, asset: str) -> BuildResult:
    """fx:<asset> BuildResult produce (dormant — live caller 0).

    load_and_build_fx_topic_payload(Redis-first per-source, DB fallback)로 payload 생성 →
    present/missing/completeness 도출. **revision vector 미포함**(B2b). loader/build 예외·payload 구조
    불량 → MALFORMED(payload None). invalid asset은 호출 계약 위반이라 ValueError(MALFORMED 아님).
    """
    if asset not in FX_TOPIC_ASSETS:
        raise ValueError(f"build_fx_result: asset '{asset}' not in {FX_TOPIC_ASSETS}")
    membership_version = fx_membership_version()
    try:
        payload = load_and_build_fx_topic_payload(db, asset)
        present = _extract_present_sources(payload)
    except Exception as e:  # loader Redis/DB 실패 or payload 구조 불량 → 발행 불가
        return BuildResult(
            asset=asset, payload=None, present_sources=(), missing_sources=(),
            membership_version=membership_version,
            completeness=BuildCompleteness.MALFORMED, build_error=repr(e),
        )
    missing = FX_MEMBERSHIP_SOURCES - present
    completeness = BuildCompleteness.COMPLETE if not missing else BuildCompleteness.PARTIAL
    return BuildResult(
        asset=asset,
        payload=payload,
        present_sources=tuple(sorted(present)),
        missing_sources=tuple(sorted(missing)),
        membership_version=membership_version,
        completeness=completeness,
        build_error=None,
    )
