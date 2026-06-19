"""P1b C6-5 — watermark Redis reader/writer (FX, dormant, behavior-change-0).

B2b coordinator hooks `watermark_reader`/`watermark_writer`(atomic_coordinator.py:192-193)가
C6-6에서 wiring될 watermark Redis I/O building block. dedicated key `watermark:fx:{asset}`
(atomic_watermark.watermark_key)에 대한 단순 GET/SET — latest:* 값의 COMPARE_WRITE_LUA(AtomicLatestWriter)와
**별 key/writer**(다중 writer race 없음 — 단일 coordinator가 per-asset lock 안에서 write).

설계 (Workflow 3-lens + codex high 의견일치):
- **client 주입 (self-fetch 아님)**: `AtomicLatestWriter`(atomic_lua.py:169) 패턴. 모듈이 live import 0이라
  **pure island** — positive `_scan_direct_live_io` trip-wire 보유(C6-3 loader는 `_get_sync_client` import라
  negative-only였던 것보다 강함). C6-6 adapter가 `_get_sync_client()`로 생성 + `asyncio.to_thread`로
  async hook(Awaitable) 만족. **dormant**: live caller 0.
- **SYNC**: AtomicLatestWriter / C6-3 loader 모두 sync redis client(decode_responses=False) 사용 — parity.
- **read fail-closed asymmetry**: read는 send **이전**(atomic_coordinator.py:291)이라 outage 시 propagate가
  안전(전체 asset 이번 cycle 포기 → clean retry; None으로 mask하면 spurious bootstrap/re-publish).
  key-miss / WatermarkSchemaError(corrupt·incompatible)만 None(정의된 bootstrap trigger).
- **write outage → False (NOT propagate)**: write는 send **이후**(atomic_coordinator.py:364, try/except 없음,
  send_performed). raise하면 marker 미저장 → watermark 영구 손실(orphan send). False 반환해야
  `if not write_ok: self._markers[asset]=real`(sent_but_uncommitted) → _recover_marker 재시도. hook 계약
  `Awaitable[bool]`과 직접 부합. asset-mismatch / serialize 실패는 caller/wiring bug라 raise(crash-early).
- **plain SET, no CAS, no TTL**: worker==1(Dockerfile --workers 1, 단일 event loop) + per-asset coordinator
  lock(atomic_coordinator.py:268)이 직렬화 → monotonicity는 coordinator의 classify_watermark_relation +
  seq floor 소유, Redis CAS 불요. persistent(allkeys-lru eviction → 다음 read miss → bootstrap).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.atomic_watermark import (
    Watermark,
    WatermarkSchemaError,
    deserialize_watermark,
    serialize_watermark,
    watermark_key,
)

logger = logging.getLogger("exchange_rate.atomic_watermark_store")


class AtomicWatermarkStore:
    """watermark:fx:{asset} 단순 GET/SET wrapper (client 주입, sync, dormant).

    C6-6 adapter가 sync `read`/`write`를 `asyncio.to_thread`로 감싸 async CoordinatorHooks를 만족.
    """

    def __init__(self, client) -> None:
        self._client = client  # 주입된 sync redis client(decode_responses=False) / 테스트는 FakeClient

    def read(self, asset: str) -> Optional[Watermark]:
        """GET watermark:fx:{asset} → Watermark | None.

        None 반환(정의된 bootstrap trigger): key-miss(eviction 포함) / WatermarkSchemaError
        (corrupt·incompatible). **Redis outage(get raises) → propagate**(outage ≠ bootstrap; read는
        send 이전이라 raise → 이번 cycle 전체 포기 후 clean retry, None mask 시 spurious bootstrap).
        """
        raw = self._client.get(watermark_key(asset))  # outage propagate (try 밖)
        if raw is None:
            return None  # key-miss / eviction → bootstrap
        try:
            return deserialize_watermark(raw)  # bytes-aware (atomic_watermark.py:153)
        except WatermarkSchemaError:
            return None  # corrupt / incompatible → fail-closed bootstrap

    def write(self, asset: str, wm: Watermark) -> bool:
        """SET watermark:fx:{asset} = serialize_watermark(wm) → True.

        Redis outage(set raises) → logger.warning + **return False**(NOT propagate): coordinator는
        write를 try/except 없이 호출하고(atomic_coordinator.py:364) send 이후라, raise하면 marker 미저장
        → watermark 영구 손실. False면 sent_but_uncommitted marker 저장 → _recover_marker 재시도.

        Raises:
            ValueError: wm.asset != asset(per-asset key vs payload mismatch = caller/wiring bug,
                crash-early — outage와 달리 deterministic이라 False로 숨기면 marker retry가 같은 bug에
                갇힘). serialize_watermark 실패도 같은 범주(try 밖 propagate).

        ⚠️ C6-6 wiring precondition (Workflow 리뷰 L2): 이 asset 가드는 write가 **send 이후**라
        **post-send backstop**이다 — current=None bootstrap 경로(reconcile asset 체크가 last_watermark
        None일 때 skip + classify NO_CURRENT가 asset 체크 전 반환)에서 miswired build_fn(asset≠param)이면
        이미 wrong-asset send 후에야 여기서 raise(orphan send). C6-6 adapter는 build_fn 직후·publisher_fn
        전에 build_result.asset == asset pre-send assertion을 추가해 orphan-send window를 닫아야 한다.
        """
        if wm.asset != asset:
            raise ValueError(
                f"AtomicWatermarkStore.write: asset/watermark mismatch — key asset={asset!r}, "
                f"wm.asset={wm.asset!r}"
            )
        # serialize는 유효 Watermark(__post_init__ 검증)에 대해 사실상 infallible — try 밖은 방어/대칭
        # (실패 시 bug라 propagate, outage False와 구분).
        value = serialize_watermark(wm)
        try:
            self._client.set(watermark_key(asset), value)
            return True
        except Exception:
            logger.warning(
                "watermark write 실패 (outage — sent_but_uncommitted 경로로 fallthrough)",
                exc_info=True,
                extra={"asset": asset},
            )
            return False
