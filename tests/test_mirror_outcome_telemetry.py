"""Slice 1a — latest mirror atomic outcome telemetry (mirror-retirement measure-first).

검증: _record_mirror_outcome 누적(atomic_outcomes label dict + cycle stats) / legacy·skip no-op /
empty atomic cycle 카운트 / get_mirror_outcome_counts interpretation 3분리(advance /
freshness_refresh=refreshed_equal[load-bearing] / redundant=skipped_newer만 / stability_concern / ratio)
+ empty 상태 + snapshot 격리. behavior-change-0(counting만) telemetry.
"""
from __future__ import annotations

import unittest

import app.latest_rates_cache as lrc


def _reset():
    lrc._mirror_outcome_counts.clear()
    for k in list(lrc._mirror_cycle_stats):
        lrc._mirror_cycle_stats[k] = 0
    lrc._mirror_outcome_last_cycle_at = None
    lrc._mirror_notable_events.clear()
    lrc._mirror_notable_by_key.clear()


def _atomic_stats(outcomes, bank=0, investing=0, failed=0,
                  index_updated=True, dxy_loaded=1, dxy_failed=0, notable=None):
    """_mirror_all_latest_atomic 산출 stats 모사 (atomic 분기)."""
    return {
        "write_mode": "atomic",
        "atomic_outcomes": dict(outcomes),
        "atomic_notable_events": list(notable) if notable else [],
        "bank": bank,
        "investing": investing,
        "failed": failed,
        "index_updated": index_updated,
        "dxy_loaded": dxy_loaded,
        "dxy_failed": dxy_failed,
    }


def _ev(label, key, source, asset="usd-krw", revision=1):
    return {"label": label, "kind": "bank", "key": key,
            "source": source, "asset": asset, "revision": revision}


class TestMirrorOutcomeTelemetry(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_accumulate_across_cycles(self):
        lrc._record_mirror_outcome(
            _atomic_stats({"advance": 2, "refreshed_equal": 28}, bank=9, investing=1))
        lrc._record_mirror_outcome(
            _atomic_stats({"refreshed_equal": 30}, bank=9, investing=1))
        snap = lrc.get_mirror_outcome_counts()
        self.assertEqual(snap["atomic_cycles"], 2)
        self.assertEqual(snap["outcomes"]["advance"], 2)
        self.assertEqual(snap["outcomes"]["refreshed_equal"], 58)
        self.assertEqual(snap["cycle_stats"]["bank_loaded"], 18)
        self.assertEqual(snap["cycle_stats"]["investing_loaded"], 2)
        self.assertEqual(snap["cycle_stats"]["index_updated"], 2)
        self.assertEqual(snap["cycle_stats"]["dxy_loaded"], 2)
        self.assertIsNotNone(snap["last_cycle_at"])

    def test_legacy_or_skip_cycle_noop(self):
        # write_mode != atomic (legacy 키 부재 / skipped_mode) → 누적 X
        lrc._record_mirror_outcome({"bank": 9})  # legacy: write_mode 키 없음
        lrc._record_mirror_outcome({"skipped_mode": "halt"})
        snap = lrc.get_mirror_outcome_counts()
        self.assertEqual(snap["atomic_cycles"], 0)
        self.assertEqual(snap["outcomes"], {})
        self.assertIsNone(snap["last_cycle_at"])

    def test_empty_atomic_cycle_counted(self):
        # codex non-blocker: outcomes 비어도 write_mode==atomic이면 cycle 카운트(빈 DB 등 숨기지 않음)
        lrc._record_mirror_outcome(_atomic_stats({}, bank=0, investing=0, dxy_loaded=0))
        snap = lrc.get_mirror_outcome_counts()
        self.assertEqual(snap["atomic_cycles"], 1)
        self.assertEqual(snap["outcomes"], {})
        self.assertIsNotNone(snap["last_cycle_at"])

    def test_interpretation_separates_freshness_from_redundant(self):
        # blocker fix: refreshed_equal=freshness_refresh(load-bearing) ≠ redundant(skipped_newer만)
        lrc._record_mirror_outcome(_atomic_stats(
            {"advance": 10, "refreshed_equal": 70, "skipped_newer": 20}))
        interp = lrc.get_mirror_outcome_counts()["interpretation"]
        self.assertEqual(interp["total_writes"], 100)
        self.assertEqual(interp["advance"], 10)
        self.assertEqual(interp["freshness_refresh"], 70)  # refreshed_equal 별도 (read-path freshness)
        self.assertEqual(interp["redundant"], 20)          # skipped_newer만 (진짜 잉여)
        self.assertEqual(interp["advance_ratio"], 0.1)
        self.assertEqual(interp["freshness_refresh_ratio"], 0.7)
        self.assertEqual(interp["redundant_ratio"], 0.2)

    def test_stability_concern_aggregation(self):
        lrc._record_mirror_outcome(_atomic_stats({
            "conflict": 1, "structural_failed": 2, "migration_required": 1,
            "invalid_schema": 1, "general_failed": 3, "advance": 5,
        }))
        interp = lrc.get_mirror_outcome_counts()["interpretation"]
        self.assertEqual(interp["stability_concern"], 5)  # 1+2+1+1 (general_failed 제외)
        self.assertEqual(interp["general_failed"], 3)      # benign retry, 별도 노출

    def test_empty_state_ratios_none(self):
        snap = lrc.get_mirror_outcome_counts()
        self.assertEqual(snap["interpretation"]["total_writes"], 0)
        self.assertIsNone(snap["interpretation"]["advance_ratio"])
        self.assertIsNone(snap["interpretation"]["redundant_ratio"])
        self.assertEqual(snap["outcomes"], {})

    def test_snapshot_isolation(self):
        lrc._record_mirror_outcome(_atomic_stats({"advance": 1}))
        snap = lrc.get_mirror_outcome_counts()
        snap["outcomes"]["advance"] = 999
        snap["cycle_stats"]["atomic_cycles"] = 999
        # 원본 오염 X
        self.assertEqual(lrc.get_mirror_outcome_counts()["outcomes"]["advance"], 1)
        self.assertEqual(lrc.get_mirror_outcome_counts()["atomic_cycles"], 1)


class TestMirrorNotableAttribution(unittest.TestCase):
    """Slice 1a-2: 희소 non-refreshed_equal event attribution (ring buffer + per-key×label)."""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_notable_captured_to_ring_and_per_key(self):
        lrc._record_mirror_outcome(_atomic_stats(
            {"advance": 1, "refreshed_equal": 29},
            bank=27, investing=3,
            notable=[_ev("advance", "latest:bank:kb:usd-krw", "kb", revision=42)]))
        snap = lrc.get_mirror_outcome_counts()
        evs = snap["recent_notable_events"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["label"], "advance")
        self.assertEqual(evs[0]["source"], "kb")
        self.assertEqual(evs[0]["revision"], 42)
        self.assertIsNotNone(evs[0]["at"])  # 기록 시각 부여
        self.assertEqual(snap["notable_by_key"]["latest:bank:kb:usd-krw"], {"advance": 1})

    def test_refreshed_equal_not_in_notable(self):
        # baseline(refreshed_equal)만인 cycle은 notable_events에 아무것도 안 남김 (writer가 notable에 미포함)
        lrc._record_mirror_outcome(_atomic_stats({"refreshed_equal": 30}, notable=[]))
        snap = lrc.get_mirror_outcome_counts()
        self.assertEqual(snap["recent_notable_events"], [])
        self.assertEqual(snap["notable_by_key"], {})

    def test_per_key_accumulates_mixed_labels(self):
        lrc._record_mirror_outcome(_atomic_stats(
            {"advance": 1, "skipped_newer": 1, "refreshed_equal": 28},
            notable=[_ev("advance", "latest:bank:kb:usd-krw", "kb"),
                     _ev("skipped_newer", "latest:bank:kb:usd-krw", "kb")]))
        lrc._record_mirror_outcome(_atomic_stats(
            {"advance": 1, "refreshed_equal": 29},
            notable=[_ev("advance", "latest:bank:kb:usd-krw", "kb")]))
        snap = lrc.get_mirror_outcome_counts()
        self.assertEqual(snap["notable_by_key"]["latest:bank:kb:usd-krw"],
                         {"advance": 2, "skipped_newer": 1})
        self.assertEqual(len(snap["recent_notable_events"]), 3)

    def test_ring_buffer_bounded(self):
        # maxlen 초과 시 오래된 것 drop (무한 성장 X)
        n = lrc._MIRROR_NOTABLE_RING_MAX + 20
        for i in range(n):
            lrc._record_mirror_outcome(_atomic_stats(
                {"advance": 1, "refreshed_equal": 29},
                notable=[_ev("advance", f"latest:bank:b{i}:usd-krw", f"b{i}")]))
        snap = lrc.get_mirror_outcome_counts()
        self.assertEqual(len(snap["recent_notable_events"]), lrc._MIRROR_NOTABLE_RING_MAX)
        # per-key는 전체 누적(key별 1건씩이라 n개 key)
        self.assertEqual(len(snap["notable_by_key"]), n)

    def test_per_key_cap_bounds_growth(self):
        # codex 019ef8d1: per-key 구조적 cap — cap 초과 새 key는 per-key skip(ring buffer는 계속 기록)
        n = lrc._MIRROR_NOTABLE_KEY_CAP + 30
        for i in range(n):
            lrc._record_mirror_outcome(_atomic_stats(
                {"advance": 1, "refreshed_equal": 29},
                notable=[_ev("advance", f"latest:bank:k{i}:usd-krw", f"k{i}")]))
        snap = lrc.get_mirror_outcome_counts()
        self.assertEqual(len(snap["notable_by_key"]), lrc._MIRROR_NOTABLE_KEY_CAP)  # 새 key cap
        self.assertEqual(len(snap["recent_notable_events"]), lrc._MIRROR_NOTABLE_RING_MAX)  # ring bound
        # cap 안에 든 기존 key는 cap 도달 후에도 계속 누적
        lrc._record_mirror_outcome(_atomic_stats(
            {"advance": 1, "refreshed_equal": 29},
            notable=[_ev("advance", "latest:bank:k0:usd-krw", "k0")]))
        self.assertEqual(
            lrc.get_mirror_outcome_counts()["notable_by_key"]["latest:bank:k0:usd-krw"]["advance"], 2)

    def test_notable_snapshot_isolation(self):
        lrc._record_mirror_outcome(_atomic_stats(
            {"advance": 1, "refreshed_equal": 29},
            notable=[_ev("advance", "latest:bank:kb:usd-krw", "kb")]))
        snap = lrc.get_mirror_outcome_counts()
        snap["recent_notable_events"].append({"label": "fake"})
        snap["notable_by_key"]["latest:bank:kb:usd-krw"]["advance"] = 999
        snap2 = lrc.get_mirror_outcome_counts()
        self.assertEqual(len(snap2["recent_notable_events"]), 1)
        self.assertEqual(snap2["notable_by_key"]["latest:bank:kb:usd-krw"]["advance"], 1)


if __name__ == "__main__":
    unittest.main()
