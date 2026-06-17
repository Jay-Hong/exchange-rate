"""P1b A3-2 — atomic Lua compare/write + v1 migration raw-CAS (§17, dormant).

§11 monotonic write의 Redis-side atomic primitive. Lua는 **semantic parsing 0** —
cjson.decode로 JSON 구조만 풀고 비교는 **string 필드(revision_key lex / rate_key eq)**만.
timestamp/rate canonical parsing은 Python(atomic_value_schema) 책임. **Lua vs WATCH = Lua**
(sync/async 서버사이드 atomic 통일).

**status-reply 계약** — compare/write Lua return 문자열:
  advance / refreshed_equal / conflict / skipped_newer / migration_required / invalid_schema
**KEYS/ARGV 계약** — KEYS=[latest_key], ARGV=[v2_value_json, incoming_revision_key, incoming_rate_key].
  advance·refreshed_equal → SET ARGV[1](incoming, 새 mirrored_at 포함) / conflict·skipped_newer → SET 안 함.

**discriminator 순서**(plan-review Medium 7 / codex): decode 실패·비-table scalar → invalid_schema /
  schema_version 부재 table(v1-object + array) → migration_required(semantic v1 검증은 migration
  command A3-3 책임) / schema_version==2 → v2 compare / 그 외(≠2·필드누락) → invalid_schema.

**dormant**: live writer는 본 모듈을 import/인스턴스화/호출하지 않음 (A4/C6 wiring). `register_script`는
  lazy — Script 객체 등록만, 첫 호출 전 SCRIPT LOAD 0. EVAL 예외/tri-state(NOT_APPLIED/UNKNOWN, §16)
  해석은 caller(A4/C6) 책임 — 본 wrapper는 thin(call→decode→outcome, 예외 propagate).

Python reference port(`evaluate_*`)는 결정 테이블 logic 단위 테스트 + 명세용. 실제 atomicity/cjson/
syntax/SET side-effect는 env-gated 실Redis 테스트가 검증 (§16 Postgres precision env-gate 선례).
"""
from __future__ import annotations

import json
from typing import Optional

from app.atomic_value_schema import SCHEMA_VERSION_V2

# ── compare/write outcome (Lua return 문자열) ──
OUTCOME_ADVANCE = "advance"
OUTCOME_REFRESHED_EQUAL = "refreshed_equal"
OUTCOME_CONFLICT = "conflict"
OUTCOME_SKIPPED_NEWER = "skipped_newer"
OUTCOME_MIGRATION_REQUIRED = "migration_required"
OUTCOME_INVALID_SCHEMA = "invalid_schema"

# ── migration raw-CAS outcome ──
MIGRATE_OUTCOME_MIGRATED = "migrated"
MIGRATE_OUTCOME_CHANGED = "changed"

# v2 atomic compare/write. cjson.decode(구조) + string compare(revision_key lex / rate_key eq).
# discriminator 순서 고정 (Medium 7). advance·refreshed_equal만 SET(ARGV[1]=incoming v2 value).
COMPARE_WRITE_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then
  redis.call('SET', KEYS[1], ARGV[1])
  return 'advance'
end
local ok, decoded = pcall(cjson.decode, current)
if not ok then
  return 'invalid_schema'
end
if type(decoded) ~= 'table' then
  return 'invalid_schema'
end
local sv = decoded.schema_version
if sv == nil then
  return 'migration_required'
end
if sv ~= 2 then
  return 'invalid_schema'
end
local cur_rev = decoded.revision_key
local cur_rate = decoded.rate_key
if type(cur_rev) ~= 'string' or type(cur_rate) ~= 'string' then
  return 'invalid_schema'
end
if ARGV[2] > cur_rev then
  redis.call('SET', KEYS[1], ARGV[1])
  return 'advance'
elseif ARGV[2] == cur_rev then
  if ARGV[3] == cur_rate then
    redis.call('SET', KEYS[1], ARGV[1])
    return 'refreshed_equal'
  else
    return 'conflict'
  end
else
  return 'skipped_newer'
end
"""

# v1 migration raw-CAS — raw string equality만(parsing 0). KEYS=[key],
# ARGV=[expected_raw_v1, new_v2_value]. expected와 현재 raw가 정확히 같을 때만 SET.
MIGRATE_CAS_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2])
  return 'migrated'
else
  return 'changed'
end
"""


def evaluate_compare_write(
    current_raw: Optional[str], incoming_revision_key: str, incoming_rate_key: str
) -> str:
    """compare/write 결정 테이블의 Python 참조 구현 — COMPARE_WRITE_LUA와 동일 outcome.

    logic 단위 테스트 + 명세용 (실 atomicity/cjson/syntax/SET은 env-gated 실Redis가 검증).
    SET 여부는 반환 outcome으로 파생(advance·refreshed_equal → SET). 비-dict/scalar는 Lua
    `type ~= 'table'`(scalar→invalid_schema) / array-table(schema_version 부재→migration_required)
    동작을 미러.
    """
    if current_raw is None:
        return OUTCOME_ADVANCE
    try:
        decoded = json.loads(current_raw)
    except (ValueError, TypeError):
        return OUTCOME_INVALID_SCHEMA  # Lua: pcall cjson.decode 실패
    if not isinstance(decoded, (dict, list)):
        return OUTCOME_INVALID_SCHEMA  # Lua: type(decoded) ~= 'table' (scalar)
    # list = Lua array table → schema_version 부재 → migration_required (semantic 검증은 A3-3)
    sv = decoded.get("schema_version") if isinstance(decoded, dict) else None
    if sv is None:
        return OUTCOME_MIGRATION_REQUIRED
    if sv != SCHEMA_VERSION_V2:
        return OUTCOME_INVALID_SCHEMA
    cur_rev = decoded.get("revision_key")
    cur_rate = decoded.get("rate_key")
    if not isinstance(cur_rev, str) or not isinstance(cur_rate, str):
        return OUTCOME_INVALID_SCHEMA
    if incoming_revision_key > cur_rev:
        return OUTCOME_ADVANCE
    if incoming_revision_key == cur_rev:
        return OUTCOME_REFRESHED_EQUAL if incoming_rate_key == cur_rate else OUTCOME_CONFLICT
    return OUTCOME_SKIPPED_NEWER


def evaluate_migrate_cas(current_raw: Optional[str], expected_raw: str) -> str:
    """migration raw-CAS 결정의 Python 참조 구현 — MIGRATE_CAS_LUA와 동일 outcome.

    raw string equality만 (현재 raw == expected → migrated, 아니면 changed). 키 부재(None)는
    expected(str)와 불일치 → changed.
    """
    return MIGRATE_OUTCOME_MIGRATED if current_raw == expected_raw else MIGRATE_OUTCOME_CHANGED


def _decode_status(result: object) -> str:
    """EVAL 반환(bytes/str)을 status 문자열로 정규화.

    bytes/str만 허용 — 그 외(async client coroutine, 예상 밖 응답)는 str()로 조용히 통과시키면
    잘못된 outcome으로 숨으므로 TypeError fail-closed (Crash Early).
    """
    if isinstance(result, bytes):
        return result.decode("utf-8")
    if isinstance(result, str):
        return result
    raise TypeError(
        f"atomic_lua EVAL 반환이 bytes/str 아님 — got {type(result).__name__} ({result!r})"
    )


class AtomicLatestWriter:
    """v2 atomic compare/write + v1 migration raw-CAS Lua wrapper (§17).

    **dormant**: live writer가 본 클래스를 인스턴스화/호출하지 않음 (A4/C6 wiring). `register_script`는
    lazy — Script 객체 + SHA만 만들고 첫 호출(EVALSHA, NOSCRIPT 시 SCRIPT LOAD) 전 server 무접촉.
    thin — call→decode→outcome 문자열. EVAL 예외는 propagate (tri-state 해석은 caller).
    """

    def __init__(self, client) -> None:
        self._client = client
        self._compare_write = client.register_script(COMPARE_WRITE_LUA)
        self._migrate_cas = client.register_script(MIGRATE_CAS_LUA)

    def compare_write(
        self, key: str, v2_value: str, revision_key: str, rate_key: str
    ) -> str:
        """v2 atomic compare/write — outcome 문자열 반환 (SET은 Lua가 atomic 수행)."""
        return _decode_status(
            self._compare_write(keys=[key], args=[v2_value, revision_key, rate_key])
        )

    def migrate_cas(self, key: str, expected_raw: str, v2_value: str) -> str:
        """v1 migration raw-CAS — migrated/changed 반환."""
        return _decode_status(
            self._migrate_cas(keys=[key], args=[expected_raw, v2_value])
        )
