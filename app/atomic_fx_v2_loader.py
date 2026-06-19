"""P1b C6-3 — single-snapshot v2 loader (payload + effective revision vector, §15:215, dormant).

B2b coordinator의 effective_resolver가 필요로 하는 **same-read** effective revision vector를 공급하는
additive loader. live `load_and_build_fx_topic_payload`와 **동일한** Redis-first + lazy DB-fallback 루프를
돌되, 각 source의 **단일 Redis GET**에서 public payload entry와 internal revision_key를 **둘 다** 뽑아낸다
(§15:215 same-read = option b, A3 COMPARE_WRITE_LUA 미수술).

설계 (codex 2-round 의견일치 + Workflow 적대 리뷰):
- **additive/behavior-change-0**: live loader/builder/read helper(`load_and_build_fx_topic_payload`,
  `build_fx_tab_payload`, `deserialize_value`, `get_latest_*_from_sync_job`)는 **무접촉**. payload는 동일
  `build_fx_tab_payload`로 조립 → byte-identical + present_sources == B2a `build_fx_result`.
- **same-read = ONE GET → 2-parse**: `deserialize_value`(public+mirrored_at, 재사용)와 `json.loads`(internal
  schema_version/revision_key/rate_key)를 **같은 raw bytes**에서. 두 번째 GET 없음.
- **effective-only, subset 허용**: vector key = v2-valid revision을 가진 source만. v1-resident/DB-fallback/
  malformed-v2 source는 payload+present엔 있되 vector에서 **omit**(fabrication·None 금지). 따라서
  `effective ⊆ present`이고 migration 진행 중/장애 시 **strict subset 가능**.
- **completeness == gate는 C6-6**: decide_and_build_next는 effective keys==present **EXACT**를 요구
  (atomic_reconcile.py ValueError)하므로, subset을 decide에 직접 먹이면 안 된다. C6-3는 faithful하게 보고만
  하고, C6-6 wiring이 effective==present일 때만 publish 진행 / 아니면 block·retry.
- **v2-valid 판정(codex amend 2)**: schema_version==2 + parse_revision_key OK + `rate_key==make_rate_key
  (public rate)` 전부 충족해야 revision 채택(아니면 omit, per-source fail-closed).
- **stale(codex amend 1)**: banks·investing **둘 다 is_stale**(live loader 정합).
- **field-survival 결합(Workflow HIGH 반영)**: revision은 `_normalize_entry`를 통과하는 entry에만
  부여(timestamp=None 등 normalize drop 대상은 omit) → effective ⊆ present 보존 + live silent-drop
  byte-identical parity(deserialize_value가 timestamp를 verbatim 통과해 None 가능).
- **schema_version type-strict**: int 2만 채택(float 2.0/bool 거부) — revision_key/rate_key isinstance
  검사와 fail-closed parity.

**dormant**: live caller 0 (AST trip-wire). C6-6이 effective_resolver로 wiring할 때까지 미호출.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from app.atomic_build import _extract_present_sources
from app.atomic_value_schema import make_rate_key, parse_revision_key
from app.crud import (
    BANK_DISPLAY_ORDER,
    select_a_latest_investing_rate_from_db,
    select_latest_bank_rates_from_db,
)
from app.fx_topic_payload import _normalize_entry, _validate_fx_asset, build_fx_tab_payload
from app.latest_rates_cache import (
    _get_sync_client,
    deserialize_value,
    is_stale,
    latest_key_bank,
    latest_key_investing,
)
from app.fx_membership import FX_MEMBERSHIP_SOURCES

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_V2_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class FxV2LoadResult:
    """same-read v2 load 결과. invariant: effective keys ⊆ present_sources (**subset** — == gate는 C6-6) /
    effective value는 parse_revision_key 통과 / present ⊆ FX_MEMBERSHIP_SOURCES.

    effective ⊊ present는 정상(migration 미완/DB-fallback/malformed-v2). C6-6이 effective==present일 때만
    decide로 진행(decide는 == 강제).
    """
    payload: Dict[str, Any]
    effective_revision_vector: Dict[str, str]
    present_sources: Tuple[str, ...]

    def __post_init__(self) -> None:
        present = set(self.present_sources)
        if not present <= FX_MEMBERSHIP_SOURCES:
            raise ValueError(
                f"FxV2LoadResult: present가 membership 초과 — {sorted(present - FX_MEMBERSHIP_SOURCES)}"
            )
        eff_keys = set(self.effective_revision_vector)
        if not eff_keys <= present:
            raise ValueError(
                f"FxV2LoadResult: effective keys가 present 초과 — {sorted(eff_keys - present)}"
            )
        for src, key in self.effective_revision_vector.items():
            if not isinstance(key, str):
                raise ValueError(f"FxV2LoadResult: effective[{src!r}] revision_key non-str — {key!r}")
            parse_revision_key(key)  # 형식 불일치 → ValueError (fail-closed)


def _extract_v2_revision(raw, public_rate: float) -> Optional[str]:
    """raw v2 value에서 revision_key 추출 — v2-valid일 때만 (codex amend 2, fail-closed).

    v2-valid = schema_version int 2(type-strict) + revision_key str·parse OK + rate_key == make_rate_key(public rate).
    v1(no schema_version)·invalid·malformed·rate_key 불일치 → None(vector omit, payload는 유지).
    public_rate는 같은 GET의 deserialize_value 결과 — same-read 정합.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    # schema_version type-strict: int 2만 (float 2.0/bool은 ==2 True여도 거부) — revision_key/rate_key
    # isinstance 검사와 fail-closed parity. serialize_v2_value는 항상 int 2 기록이라 false-omit 없음.
    schema = obj.get("schema_version")
    if type(schema) is not int or schema != _V2_SCHEMA_VERSION:
        return None  # v1(schema_version 부재)/wrong/float·bool → fail-closed
    rev = obj.get("revision_key")
    if not isinstance(rev, str):
        return None
    try:
        parse_revision_key(rev)
    except ValueError:
        return None
    # rate_key 무결성: internal rate_key가 public rate와 정합해야 (tamper/drift fail-closed)
    rate_key = obj.get("rate_key")
    if not isinstance(rate_key, str):
        return None
    try:
        expected_rate_key = make_rate_key(public_rate)
    except ValueError:
        return None
    if rate_key != expected_rate_key:
        return None
    return rev


def _read_v2_source(client, key: str, source: str, asset: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """단일 Redis GET → (public_entry, revision_key|None). miss/stale/parse-fail → (None, None).

    same-read: 같은 raw bytes에서 deserialize_value(public+mirrored_at, 재사용)와 _extract_v2_revision
    (internal). stale 정책은 live helper와 동일(is_stale). public entry shape는 get_latest_*_from_sync_job와
    동일({source, asset, rate, timestamp})이라 build_fx_tab_payload에 그대로.
    """
    try:
        raw = client.get(key)
    except Exception:
        return None, None
    if raw is None:
        return None, None
    public = deserialize_value(raw)  # 재사용 — public {rate, timestamp, mirrored_at}
    if public is None:
        return None, None
    if is_stale(public["mirrored_at"]):  # banks·investing 둘 다 (codex amend 1)
        return None, None
    entry = {"source": source, "asset": asset, "rate": public["rate"], "timestamp": public["timestamp"]}
    # field-survival 결합(Workflow HIGH): revision은 build_fx_tab_payload의 _normalize_entry를 통과하는
    # entry에만 부여해야 effective ⊆ present invariant가 유지된다. deserialize_value가 timestamp를
    # verbatim 통과(None 가능)하므로 normalize에서 drop될 entry는 present에서 빠진다 → revision도 omit.
    # entry 자체는 유지 → live(append→normalize drop, DB-fallback 미유발)와 byte-identical parity.
    # _normalize_entry 재사용으로 향후 normalize 필드 추가에도 자동 동기(중복 가드 회피).
    if _normalize_entry(entry, fallback_source=source, fallback_asset=asset) is None:
        return entry, None
    revision = _extract_v2_revision(raw, public["rate"])  # v2-valid면 revision, 아니면 None
    return entry, revision


def load_fx_topic_payload_with_revisions(db: "Session", asset: str) -> FxV2LoadResult:
    """FX 데이터 load + same-read effective revision vector (§15:215, dormant — live caller 0).

    live `load_and_build_fx_topic_payload`와 **동일 구조**(Redis-first per-source + lazy DB-fallback +
    build_fx_tab_payload). 차이: Redis read를 v2-aware(_read_v2_source)로 해서 같은 GET에서 revision도 수집.
    DB-fallback/v1/malformed source는 payload엔 들어가나 effective vector엔 omit(effective ⊆ present).
    """
    _validate_fx_asset(asset)
    client = _get_sync_client()

    bank_rates: List[Dict[str, Any]] = []
    effective: Dict[str, str] = {}
    _db_banks_loaded: Optional[Dict[str, Dict[str, Any]]] = None
    for bank_source in BANK_DISPLAY_ORDER:
        entry: Optional[Dict[str, Any]] = None
        revision: Optional[str] = None
        if client is not None:
            entry, revision = _read_v2_source(client, latest_key_bank(bank_source, asset), bank_source, asset)
        if entry is not None:
            bank_rates.append(entry)
            if revision is not None:
                effective[bank_source] = revision
            continue
        # Redis miss/stale → DB fallback (payload only, revision 없음 — lazy 1회)
        if _db_banks_loaded is None:
            _db_banks_loaded = {row["bank"]: row for row in select_latest_bank_rates_from_db(db, asset)}
        db_row = _db_banks_loaded.get(bank_source)
        if db_row is not None:
            bank_rates.append(db_row)

    # Investing reference — Redis-first 단일 fallback
    reference: Optional[Dict[str, Any]] = None
    if client is not None:
        ref_entry, ref_revision = _read_v2_source(client, latest_key_investing(asset), "investing", asset)
        if ref_entry is not None:
            reference = ref_entry
            if ref_revision is not None:
                effective["investing"] = ref_revision
    if reference is None:
        reference = select_a_latest_investing_rate_from_db(db, asset)  # DB-fallback, revision 없음

    payload = build_fx_tab_payload(asset, bank_rates, reference)  # 재사용(불변) → byte-identical
    present_sources = tuple(sorted(_extract_present_sources(payload)))  # B2a와 동일 추출
    # DB-fallback이 payload에 넣은 source가 effective에 없을 수 있음(effective ⊆ present, subset 허용).
    return FxV2LoadResult(
        payload=payload, effective_revision_vector=effective, present_sources=present_sources
    )
