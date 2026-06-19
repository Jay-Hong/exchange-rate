"""P1b C6-9a — build_cutover_status_dict 단위 테스트 (read-only go/no-go status, never-crash).

shape(config/cutover/gate_shadow/future) + cutover block(read_cutover_snapshot_fresh) + gate_shadow(C6-7 surface)
+ future available:false(no fabrication) + per-block never-crash(cutover/gate 격리) + pure(refresh_from_db 미호출).
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import atomic_cutover_runtime, atomic_cutover_status
from app.atomic_cutover import CutoverState
from app.atomic_cutover_runtime import CutoverReadinessSnapshot
from app.atomic_cutover_status import build_cutover_status_dict
from app.atomic_write_control import WriterMode


def _snap(*, read_ok=True, state=CutoverState.LEGACY_READY, gate_open=True):
    return CutoverReadinessSnapshot(
        writer_enforced_action=WriterMode.LEGACY, cutover_state=state,
        bootstrap_session_id="s:1", bootstrap_generation=4, bootstrap_status="idle",
        per_asset_publish_state=(("usd-krw", "blocked"), ("jpy-krw", "blocked")),
        publisher_gate_open=gate_open, read_ok=read_ok,
    )


def _telem(gate_would_block=0):
    return {f"fx:{a}": {
        "gate_would_block_dry_run": gate_would_block, "gate_last_disposition": None,
        "gate_last_at_kst": None, "last_result": "sent",  # legacy field (gate_shadow엔 포함 안 됨)
    } for a in ("usd-krw", "jpy-krw", "eur-krw")}


class TestBuildCutoverStatus(unittest.IsolatedAsyncioTestCase):

    async def _build(self, *, snap_fn=None, telem_fn=None, db_factory=None):
        snap_fn = snap_fn or (lambda db: _snap())
        telem_fn = telem_fn or AsyncMock(return_value=_telem())
        df = db_factory or (lambda: MagicMock())
        with patch.object(atomic_cutover_runtime, "read_cutover_snapshot_fresh", snap_fn), \
             patch.object(atomic_cutover_status, "get_fx_topic_telemetry", telem_fn):
            return await build_cutover_status_dict(df)

    async def test_shape(self):
        r = await self._build()
        self.assertEqual(r["status"], "ok")
        for block in ("config", "cutover", "gate_shadow", "future"):
            self.assertIn(block, r)

    async def test_cutover_block(self):
        r = await self._build(snap_fn=lambda db: _snap(state=CutoverState.LEGACY_READY))
        c = r["cutover"]
        self.assertEqual(c["cutover_state"], "legacy_ready")
        self.assertTrue(c["read_ok"])
        self.assertTrue(c["publisher_gate_open"])
        self.assertEqual(c["bootstrap_generation"], 4)
        self.assertEqual(c["per_asset_publish_state"], {"usd-krw": "blocked", "jpy-krw": "blocked"})

    async def test_gate_shadow_block_only_gate_fields(self):
        r = await self._build(telem_fn=AsyncMock(return_value=_telem(gate_would_block=3)))
        gs = r["gate_shadow"]
        self.assertEqual(gs["fx:usd-krw"]["gate_would_block_dry_run"], 3)
        # legacy field(last_result)는 gate_shadow에 포함 안 됨 (gate_* 만 projection)
        self.assertNotIn("last_result", gs["fx:usd-krw"])
        self.assertIn("gate_shadow_note", r)

    async def test_future_available_false(self):
        r = await self._build()
        for k in ("conflict_counters", "migration_verification", "watermark_lag"):
            self.assertFalse(r["future"][k]["available"])
            self.assertIn("reason", r["future"][k])

    async def test_pure_read_uses_fresh_not_refresh(self):
        # build는 refresh_from_db(_current mutate)가 아니라 read_cutover_snapshot_fresh(pure) 사용
        fresh = MagicMock(return_value=_snap())
        with patch.object(atomic_cutover_runtime, "read_cutover_snapshot_fresh", fresh), \
             patch.object(atomic_cutover_runtime, "refresh_from_db") as refresh, \
             patch.object(atomic_cutover_status, "get_fx_topic_telemetry", AsyncMock(return_value=_telem())):
            await build_cutover_status_dict(lambda: MagicMock())
        fresh.assert_called_once()
        refresh.assert_not_called()   # _current 미변경 보장

    async def test_never_crash_cutover_block(self):
        # db_factory 예외 → cutover_read_error, 나머지 block 정상 (per-block isolation)
        def boom():
            raise RuntimeError("session down")
        r = await self._build(db_factory=boom)
        self.assertIn("cutover_read_error", r["cutover"])
        self.assertEqual(r["status"], "degraded")   # block error → top-level degraded (A1 정합)
        self.assertIn("config", r)
        self.assertIn("gate_shadow", r)   # 다른 block 영향 0

    async def test_never_crash_gate_shadow_block(self):
        r = await self._build(telem_fn=AsyncMock(side_effect=RuntimeError("redis down")))
        self.assertEqual(r["gate_shadow"], {"error": "gate_shadow_read_error"})
        self.assertEqual(r["status"], "degraded")
        self.assertIn("cutover", r)       # 다른 block 영향 0
        self.assertIn("gate_shadow_note", r)   # note는 error path에서도 항상 present


if __name__ == "__main__":
    unittest.main()
