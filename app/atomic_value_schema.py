"""P1b A3-1 — v2 Redis value schema serialization (§17, pure stdlib, dormant).

§11 monotonic atomic write가 쓸 v2 value 포맷. Lua는 semantic parsing 0 — Python이
canonical 인코딩(revision_key fixed-width / rate_key canonical decimal)을 만들고 Lua는
string compare만 (§17 책임 분리).

**v2 = 기존 public 포맷에 additive**:
- public (기존 UNCHANGED — WS/API 소비자 문자열 계약): `rate` / `timestamp`(ISO) / `mirrored_at`(ISO)
- internal (atomic 전용, old reader 무시 — §12-7 id 미유출): `schema_version`=2 / `revision_key` / `rate_key`

**no Redis/crud/DB/네트워크 직접 의존** — `app.atomic_revision`의 `Revision`만 import
(stdlib-equivalent revision primitive). ⚠️ app 패키지 import는 `app/__init__.py` logging
초기화를 transitive하게 타지만 그건 모든 app 모듈 공유라 본 모듈 고유 side-effect 아님.
**dormant**: live writer는 본 모듈을 호출하지 않음 (A3 Lua 호출 + 배선은 후속, behavior-change-0).
USDT 5b-bis 같은 source별 추가 public field는 wiring-time(A4/C6) 관심사 — 본 base 직렬화 범위 밖.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from decimal import Decimal
from typing import Optional

from app.atomic_revision import Revision

# v2 discriminator (JSON number; v1 = 부재/≠2). §17.
SCHEMA_VERSION_V2 = 2

# revision_key 고정폭 — epoch_us·id 각 20자리 zero-pad. lex(string) compare가 numeric
# order와 일치하려면 **동일 폭 + non-negative + 상한 내(폭 초과 금지)** 필수.
# 20자리 상한 10^20 = ~3.17M년 분량 μs — real epoch_us(~1.7e15, 16자리)·DB bigint
# id(<9.2e18, 19자리) 모두 수용. 10^20 이상은 폭 초과로 lex 깨짐 → fail-closed.
_REVISION_KEY_WIDTH = 20
_REVISION_KEY_MAX = 10 ** _REVISION_KEY_WIDTH


def make_revision_key(epoch_us: int, row_id: int) -> str:
    """revision `(epoch_us, id)` → fixed-width lex-comparable key `"{epoch:020d}:{id:020d}"`.

    Lua가 lex(string) compare로 monotonic 판정하므로 zero-pad 고정폭 + **non-negative** 강제.
    음수 epoch는 `{:020d}`에서 lex order가 numeric과 반대로 깨짐 → fail-closed(ValueError).
    canonical 함수(to_canonical_epoch_us)는 음수를 일반 지원하지만(§16), revision_key는 real
    rate data(항상 post-1970 양수 epoch + 양수 id) 전용 인코딩이라 음수는 corrupt/불가.
    """
    if isinstance(epoch_us, bool) or not isinstance(epoch_us, int):
        raise ValueError(f"make_revision_key: epoch_us must be int — got {epoch_us!r}")
    if isinstance(row_id, bool) or not isinstance(row_id, int):
        raise ValueError(f"make_revision_key: row_id must be int — got {row_id!r}")
    # [0, 10^20) — 음수는 lex order 반전, 10^20 이상은 폭 초과(>20자리)로 lex 깨짐 → fail-closed.
    if not (0 <= epoch_us < _REVISION_KEY_MAX):
        raise ValueError(
            f"make_revision_key: epoch_us는 [0, {_REVISION_KEY_MAX}) — got {epoch_us}"
        )
    if not (0 <= row_id < _REVISION_KEY_MAX):
        raise ValueError(
            f"make_revision_key: row_id는 [0, {_REVISION_KEY_MAX}) — got {row_id}"
        )
    return f"{epoch_us:0{_REVISION_KEY_WIDTH}d}:{row_id:0{_REVISION_KEY_WIDTH}d}"


def make_revision_key_from_revision(revision: Revision) -> str:
    """`Revision` tuple → revision_key (make_revision_key(epoch_us, id))."""
    return make_revision_key(revision[0], revision[1])


def parse_revision_key(key: str) -> Revision:
    """revision_key `"{epoch:020d}:{id:020d}"` → `Revision (epoch_us, id)` (make_revision_key 역함수).

    인코딩 single-source 원칙 — B1/B2b(D layer) success watermark의 present_revision_vector(string)와
    DB Revision(tuple) 비교 시 양쪽이 동일 인코딩을 공유하도록 역변환을 여기(인코딩 소유 모듈)에 둔다.
    **round-trip 보장**: `make_revision_key(*parse_revision_key(k)) == k` (canonical 양수·고정폭 한정).
    **fail-closed**: 비-str / 'epoch:id' 형식 아님 / 20자리 고정폭 아님 / ascii 숫자 아님(부호·공백·unicode
    digit 불가) / 범위 초과 → ValueError. make_revision_key가 음수/폭초과를 거부하므로 역함수도 대칭.
    """
    if not isinstance(key, str):
        raise ValueError(f"parse_revision_key: str만 허용 — got {key!r}")
    parts = key.split(":")
    if len(parts) != 2:
        raise ValueError(f"parse_revision_key: 'epoch:id' 형식 아님 — got {key!r}")
    epoch_str, id_str = parts
    # 고정폭 20자리 + ascii digit (str.isdigit은 unicode digit도 True라 isascii 병행 — int() 새는 것 차단)
    for part in (epoch_str, id_str):
        if len(part) != _REVISION_KEY_WIDTH or not (part.isascii() and part.isdigit()):
            raise ValueError(
                f"parse_revision_key: 각 부분 {_REVISION_KEY_WIDTH}자리 ascii 숫자 고정폭 아님 — got {key!r}"
            )
    epoch_us = int(epoch_str)
    row_id = int(id_str)
    if not (0 <= epoch_us < _REVISION_KEY_MAX and 0 <= row_id < _REVISION_KEY_MAX):
        raise ValueError(f"parse_revision_key: 범위 초과 — got {key!r}")
    return (epoch_us, row_id)


def make_rate_key(rate: float) -> str:
    """rate(float) → canonical decimal string (trailing-zero 제거, **exponent 금지**).

    Lua는 rate_key를 string equality로만 비교 → 같은 수치가 항상 같은 문자열이어야 함.
    `Decimal(str(rate))`로 float binary 잔차 회피(§16/quantize 교훈), `.normalize()`로
    trailing-zero 제거, `format(..., 'f')`로 exponent 금지(normalize가 `1E+3` 만드는 것 차단).

    **fail-closed (Crash Early)**: bool/non-numeric/NaN/Infinity/음수 → ValueError(일관된 실패
    모드 — bool은 Decimal(str)에서 InvalidOperation, str/None은 TypeError로 새는 것 차단).
    signed zero(-0.0)는 `"0"`.
    """
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise ValueError(f"make_rate_key: int/float만 허용 — got {rate!r}")
    if not math.isfinite(rate):
        raise ValueError(f"make_rate_key: 유한값만 허용 — got {rate!r}")
    d = Decimal(str(rate))
    if d < 0:
        raise ValueError(f"make_rate_key: non-negative rate만 허용 — got {rate!r}")
    if d == 0:
        d = Decimal(0)  # -0.0 / 0.0 → "0" (signed zero 정규화)
    return format(d.normalize(), "f")


def serialize_v2_value(
    *,
    rate: float,
    timestamp: str,
    mirrored_at: datetime,
    revision: Revision,
    source: Optional[str] = None,
    asset: Optional[str] = None,
) -> str:
    """v2 Redis value JSON 직렬화 — public(기존 UNCHANGED) + internal additive (§17).

    Args:
        rate: 환율 (public, v1과 동일 float)
        timestamp: 환율 발생 시각 ISO 문자열 (public, v1과 동일 — fixed-width 변환 금지)
        mirrored_at: mirror/write 시각 tz-aware datetime (public, .isoformat() — v1 serialize_value 정합)
        revision: `(canonical_epoch_us, id)` — internal revision_key 생성용 (payload에 raw id 미노출)
        source/asset: debug optional (internal)
    """
    # mirrored_at은 tz-aware여야 — naive면 old reader의 deserialize_value(naive mirrored_at → None)가
    # v2를 못 읽음 (A3-3 review L2, fail-closed Crash Early).
    if mirrored_at.tzinfo is None or mirrored_at.tzinfo.utcoffset(mirrored_at) is None:
        raise ValueError(f"serialize_v2_value: mirrored_at must be tz-aware — got {mirrored_at!r}")
    payload = {
        # public — v1 serialize_value와 동일 키/의미 (소비자 계약 불변)
        "rate": rate,
        "timestamp": timestamp,
        "mirrored_at": mirrored_at.isoformat(),
        # internal — atomic 전용 (old reader 무시, §12-7 id 미유출)
        "schema_version": SCHEMA_VERSION_V2,
        "revision_key": make_revision_key_from_revision(revision),
        "rate_key": make_rate_key(rate),
    }
    if source is not None:
        payload["source"] = source
    if asset is not None:
        payload["asset"] = asset
    return json.dumps(payload, ensure_ascii=False)
