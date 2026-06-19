"""P1b C6-5b-4 — mirror(_mirror_all_latest) atomicization 행동 테스트.

ATOMIC mode에서 mirror가 bank/investing data key를 v2 compare_write로 적재하는 분기 검증:
- LEGACY byte-identity (build_atomic_writer/compare_write 미호출, v1 serialize_value)
- outcome→stats 매핑 (advance/refreshed_equal/skipped_newer=loaded / conflict/structural/general=failed)
- **all-skipped_newer → index_updated=True, failed=0** (busy cycle index 억제 안 됨 — 핵심 lock)
- **any-conflict → index 미갱신, failed>0** (corruption 시 fail-closed)
- attempted_total = loaded_total + failed (전 outcome invariant)
- DXY 양 모드 v1 유지 (compare_write 미경유)
- HALT → full quiesce(write 0, skipped_mode 마커)
- revision parity (RevisionedRate.revision → revision_key) + public timestamp byte-identity(D4)
- bank display order (codex catch — revision selector 표시순 미적용 → _bank_display_sort_key 명시 정렬)
- async/sync bridge: compare_write가 to_thread(다른 스레드)에서 실행 (D2)

AST trip-wire(write-mode-aware)는 test_atomic_write_runtime, island helper 단위는 test_atomic_direct_write.
"""
from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app import crud
from app.atomic_lua import (
    OUTCOME_ADVANCE,
    OUTCOME_CONFLICT,
    OUTCOME_INVALID_SCHEMA,
    OUTCOME_MIGRATION_REQUIRED,
    OUTCOME_REFRESHED_EQUAL,
    OUTCOME_SKIPPED_NEWER,
)
from app.atomic_revision import RevisionedRate
from app.atomic_value_schema import make_revision_key_from_revision
from app.atomic_write_control import WriterMode
from app.atomic_write_runtime import WriteModeSnapshot
from app.latest_rates_cache import (
    LATEST_DXY_KEY,
    LATEST_INDEX_KEY,
    _mirror_all_latest,
    latest_key_bank,
    latest_key_investing,
)

_TS = datetime(2026, 6, 20, 1, 0, 0)  # naive UTC (to_kst_isoformat가 KST 변환)


def _snap(enforced: str) -> WriteModeSnapshot:
    return WriteModeSnapshot(
        diagnostic_effective_mode=enforced,
        activation_latched=(enforced != WriterMode.LEGACY),
        enforced_action=enforced,
        mode_generation=0,
    )


def _bank_rev(bank: str, rate: float, rid: int) -> RevisionedRate:
    return RevisionedRate(source=bank, asset="usd-krw", rate=rate,
                          timestamp=_TS, revision=(1_700_000_000_000_000, rid))


def _inv_rev(rate: float, rid: int) -> RevisionedRate:
    return RevisionedRate(source="investing", asset="usd-krw", rate=rate,
                          timestamp=_TS, revision=(1_700_000_000_000_000, rid))


class _FakeWriter:
    """compare_write 시그니처 mirror — 고정 outcome 반환 + 호출 인자/스레드 기록."""

    def __init__(self, outcome: str = OUTCOME_ADVANCE) -> None:
        self.outcome = outcome
        self.calls = []           # (key, v2_value, revision_key, rate_key)
        self.thread_ids = set()

    def compare_write(self, key, v2_value, revision_key, rate_key):
        self.calls.append((key, v2_value, revision_key, rate_key))
        self.thread_ids.add(threading.get_ident())
        return self.outcome


class _AtomicMirrorBase(unittest.IsolatedAsyncioTestCase):
    async def _run_atomic(self, *, banks, investing, outcome=OUTCOME_ADVANCE, dxy=None, set_ok=True):
        fake = _FakeWriter(outcome)
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), \
             patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]), \
             patch("app.crud._select_latest_investing_rate_with_revision", return_value=investing), \
             patch("app.crud._select_latest_bank_rates_with_revision", return_value=list(banks)), \
             patch("app.crud.get_latest_dxy_rate", return_value=dxy), \
             patch("app.atomic_direct_write.build_atomic_writer", return_value=fake), \
             patch("app.latest_rates_cache._set_latest", new_callable=AsyncMock) as mock_set:
            mock_set.return_value = set_ok
            stats = await _mirror_all_latest(MagicMock())
        return stats, fake, mock_set


class TestAtomicOutcomeMapping(_AtomicMirrorBase):

    async def test_advance_all_loaded_index_updated(self):
        banks = [_bank_rev("kb", 1500.0, 1), _bank_rev("hana", 1501.0, 2)]
        stats, fake, mock_set = await self._run_atomic(banks=banks, investing=_inv_rev(1499.0, 3))
        self.assertEqual(stats["write_mode"], "atomic")
        self.assertEqual(stats["attempted_total"], 3)
        self.assertEqual(stats["loaded_total"], 3)
        self.assertEqual(stats["bank"], 2)
        self.assertEqual(stats["investing"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertTrue(stats["index_updated"])
        self.assertEqual(stats["atomic_outcomes"], {"advance": 3})
        # compare_write가 3 data key에 호출 (index는 _set_latest, compare_write 아님)
        self.assertEqual(len(fake.calls), 3)
        # index가 _set_latest로 v1 기록
        index_calls = [c for c in mock_set.call_args_list if c[0][0] == LATEST_INDEX_KEY]
        self.assertEqual(len(index_calls), 1)

    async def test_refreshed_equal_loaded(self):
        banks = [_bank_rev("kb", 1500.0, 1)]
        stats, _, _ = await self._run_atomic(banks=banks, investing=None, outcome=OUTCOME_REFRESHED_EQUAL)
        self.assertEqual(stats["loaded_total"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertTrue(stats["index_updated"])
        self.assertEqual(stats["atomic_outcomes"], {"refreshed_equal": 1})

    async def test_all_skipped_newer_index_updated_no_failed(self):
        # 핵심 lock: skipped_newer는 present(loaded)라 busy cycle에도 index 갱신 + failed=0.
        banks = [_bank_rev("kb", 1500.0, 1), _bank_rev("hana", 1501.0, 2)]
        stats, _, mock_set = await self._run_atomic(
            banks=banks, investing=_inv_rev(1499.0, 3), outcome=OUTCOME_SKIPPED_NEWER
        )
        self.assertEqual(stats["loaded_total"], 3)
        self.assertEqual(stats["failed"], 0)
        self.assertTrue(stats["index_updated"], "skipped_newer가 index를 억제하면 안 됨")
        self.assertEqual(stats["atomic_outcomes"], {"skipped_newer": 3})
        index_calls = [c for c in mock_set.call_args_list if c[0][0] == LATEST_INDEX_KEY]
        self.assertEqual(len(index_calls), 1)

    async def test_conflict_suppresses_index(self):
        # 핵심 lock: conflict(corruption)는 failed → index 게이트(failed==0) 차단.
        banks = [_bank_rev("kb", 1500.0, 1)]
        stats, _, mock_set = await self._run_atomic(banks=banks, investing=None, outcome=OUTCOME_CONFLICT)
        self.assertEqual(stats["loaded_total"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertFalse(stats["index_updated"])
        self.assertEqual(stats["atomic_outcomes"], {"conflict": 1})
        index_calls = [c for c in mock_set.call_args_list if c[0][0] == LATEST_INDEX_KEY]
        self.assertEqual(len(index_calls), 0, "conflict 시 index SET 안 됨")

    async def test_migration_required_structural_failed(self):
        banks = [_bank_rev("kb", 1500.0, 1)]
        stats, _, _ = await self._run_atomic(banks=banks, investing=None, outcome=OUTCOME_MIGRATION_REQUIRED)
        self.assertEqual(stats["failed"], 1)
        self.assertFalse(stats["index_updated"])
        self.assertEqual(stats["atomic_outcomes"], {"migration_required": 1})

    async def test_invalid_schema_structural_failed(self):
        banks = [_bank_rev("kb", 1500.0, 1)]
        stats, _, _ = await self._run_atomic(banks=banks, investing=None, outcome=OUTCOME_INVALID_SCHEMA)
        self.assertEqual(stats["failed"], 1)
        self.assertFalse(stats["index_updated"])
        self.assertEqual(stats["atomic_outcomes"], {"invalid_schema": 1})

    async def test_invariant_attempted_eq_loaded_plus_failed(self):
        banks = [_bank_rev("kb", 1500.0, 1), _bank_rev("hana", 1501.0, 2)]
        for outcome in (OUTCOME_ADVANCE, OUTCOME_REFRESHED_EQUAL, OUTCOME_SKIPPED_NEWER,
                        OUTCOME_CONFLICT, OUTCOME_MIGRATION_REQUIRED, OUTCOME_INVALID_SCHEMA):
            stats, _, _ = await self._run_atomic(banks=banks, investing=_inv_rev(1499.0, 3), outcome=outcome)
            self.assertEqual(
                stats["attempted_total"], stats["loaded_total"] + stats["failed"],
                f"invariant 깨짐 @ {outcome}",
            )
            self.assertEqual(stats["loaded_total"], stats["bank"] + stats["investing"] + stats["source"])


class TestAtomicBoundary(_AtomicMirrorBase):

    async def test_dxy_stays_v1_in_atomic(self):
        # DXY는 atomic mode에서도 v1 serialize_dxy_value (compare_write 미경유, schema_version 없음).
        dxy = {"rate": 99.2, "timestamp": "2026-06-20T10:00:00+09:00", "source": "investing"}
        stats, fake, mock_set = await self._run_atomic(
            banks=[_bank_rev("kb", 1500.0, 1)], investing=None, dxy=dxy
        )
        self.assertEqual(stats["dxy_attempted"], 1)
        self.assertEqual(stats["dxy_loaded"], 1)
        dxy_calls = [c for c in mock_set.call_args_list if c[0][0] == LATEST_DXY_KEY]
        self.assertEqual(len(dxy_calls), 1)
        dxy_payload = json.loads(dxy_calls[0][0][1])
        self.assertIn("source", dxy_payload)               # v1 dxy schema
        self.assertNotIn("schema_version", dxy_payload)     # v2 아님
        # DXY key는 compare_write로 가지 않음
        self.assertNotIn(LATEST_DXY_KEY, [c[0] for c in fake.calls])

    async def test_revision_parity_and_public_timestamp(self):
        rev = _bank_rev("kb", 1500.0, 7)
        stats, fake, _ = await self._run_atomic(banks=[rev], investing=None)
        key, v2_value, revision_key, rate_key = fake.calls[0]
        self.assertEqual(key, latest_key_bank("kb", "usd-krw"))
        # revision parity — RevisionedRate.revision → revision_key (direct flush-ref와 same row→same rev)
        self.assertEqual(revision_key, make_revision_key_from_revision(rev.revision))
        # D4 public timestamp byte-identity — to_kst_isoformat(raw datetime)
        payload = json.loads(v2_value)
        self.assertEqual(payload["timestamp"], crud.to_kst_isoformat(_TS))
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["rate"], 1500.0)

    async def test_bank_display_order_matches_legacy(self):
        # codex catch: revision selector는 표시순 미적용 → mirror가 _bank_display_sort_key로 정렬해야
        # loaded_keys 순서가 legacy와 동일. 비-표시순 입력 → 표시순 출력 검증.
        banks = [_bank_rev("citi", 1.0, 1), _bank_rev("kb", 2.0, 2), _bank_rev("hana", 3.0, 3)]
        _, fake, _ = await self._run_atomic(banks=banks, investing=None)
        written_banks = [c[0] for c in fake.calls]  # compare_write key 순서
        expected = sorted(
            (latest_key_bank(b.source, "usd-krw") for b in banks),
            key=lambda k: crud._bank_display_sort_key(k.split(":")[2]),
        )
        # display order = BANK_DISPLAY_ORDER 기준 (kb→hana→...→citi)
        self.assertEqual(
            written_banks,
            [latest_key_bank(b, "usd-krw") for b in sorted(
                ["citi", "kb", "hana"], key=crud._bank_display_sort_key)],
        )
        self.assertEqual(written_banks, expected)

    async def test_investing_then_banks_interleave(self):
        # loaded_keys 순서 = pair별 investing → display-sorted banks (legacy 인터리브).
        banks = [_bank_rev("kb", 1500.0, 1)]
        _, fake, _ = await self._run_atomic(banks=banks, investing=_inv_rev(1499.0, 2))
        keys = [c[0] for c in fake.calls]
        self.assertEqual(keys[0], latest_key_investing("usd-krw"))   # investing 먼저
        self.assertEqual(keys[1], latest_key_bank("kb", "usd-krw"))

    async def test_compare_write_runs_in_worker_thread(self):
        # D2 bridge: 동기 compare_write가 asyncio.to_thread(다른 스레드)에서 실행 (event loop non-block).
        banks = [_bank_rev("kb", 1500.0, 1)]
        _, fake, _ = await self._run_atomic(banks=banks, investing=None)
        self.assertEqual(len(fake.thread_ids), 1)
        self.assertNotIn(threading.get_ident(), fake.thread_ids, "compare_write가 event loop 스레드에서 실행됨")


class TestModeGate(_AtomicMirrorBase):

    async def test_halt_full_quiesce_no_write(self):
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.HALT)), \
             patch("app.atomic_direct_write.build_atomic_writer") as mock_build, \
             patch("app.latest_rates_cache._set_latest", new_callable=AsyncMock) as mock_set:
            stats = await _mirror_all_latest(MagicMock())
        self.assertEqual(stats["skipped_mode"], WriterMode.HALT)
        self.assertEqual(stats["attempted_total"], 0)
        self.assertEqual(stats["loaded_total"], 0)
        self.assertEqual(stats["failed"], 0)
        self.assertFalse(stats["index_updated"])
        self.assertEqual(stats["dxy_attempted"], 0)
        mock_build.assert_not_called()         # atomic writer 생성 0
        mock_set.assert_not_called()           # bank/investing/index/DXY 전부 write 0

    async def test_legacy_path_does_not_touch_atomic(self):
        # LEGACY → 기존 v1 경로 (build_atomic_writer 미호출, serialize_value, write_mode 키 없음).
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)), \
             patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]), \
             patch("app.crud.select_a_latest_investing_rate_from_db", return_value=None), \
             patch("app.crud.select_latest_bank_rates_from_db",
                   return_value=[{"currency": "usd-krw", "bank": "kb", "rate": 1500.0,
                                  "timestamp": "2026-06-20T10:00:00+09:00"}]), \
             patch("app.crud.get_source_rates_as_legacy_format", return_value=[]), \
             patch("app.crud.get_latest_dxy_rate", return_value=None), \
             patch("app.atomic_direct_write.build_atomic_writer") as mock_build, \
             patch("app.latest_rates_cache._set_latest", new_callable=AsyncMock) as mock_set:
            mock_set.return_value = True
            stats = await _mirror_all_latest(MagicMock())
        mock_build.assert_not_called()
        self.assertNotIn("write_mode", stats)          # legacy stats엔 atomic 마커 없음
        self.assertNotIn("skipped_mode", stats)
        self.assertEqual(stats["bank"], 1)
        # v1 serialize_value (schema_version 없음)
        bank_call = [c for c in mock_set.call_args_list
                     if c[0][0] == latest_key_bank("kb", "usd-krw")][0]
        self.assertNotIn("schema_version", json.loads(bank_call[0][1]))


if __name__ == "__main__":
    unittest.main()
