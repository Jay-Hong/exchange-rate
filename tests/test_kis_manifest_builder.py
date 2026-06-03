"""Step 4B 단위 2 — manifest partition builder (segment-clip fetch + gate) 단위 테스트.

fetch_fn 주입 → mock payload (network 의존 0).
hard fail (boundary=next / segment 밖 / window 밖 / dup) vs surface (gap / cap) 분리.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_kis_source_daily_rates as B  # noqa: E402


def _row(d: date, contract_code: str, close: str = "1500.0") -> dict:
    return {"date_kst": d, "contract_code": contract_code, "close": close}


def _fetch_from(mapping):
    """contract.short_code → list[row] mock fetch_fn."""
    def fetch_fn(contract, fetch_start, fetch_end):
        return list(mapping.get(contract.short_code, []))
    return fetch_fn


class TestSegmentFetchRange(unittest.TestCase):
    def test_first_segment_clips_to_window_start(self):
        # seg_start < window_start → fetch_start = window_start, fetch_end = expiry-1 (만기일 제외)
        fs, fe = B._segment_fetch_range(date(2026, 4, 20), date(2026, 5, 18),
                                        date(2026, 4, 25), date(2026, 6, 10))
        self.assertEqual(fs, date(2026, 4, 25))
        self.assertEqual(fe, date(2026, 5, 17))

    def test_last_segment_clips_to_window_end(self):
        fs, fe = B._segment_fetch_range(date(2026, 5, 18), date(2026, 6, 15),
                                        date(2026, 4, 25), date(2026, 6, 10))
        self.assertEqual(fs, date(2026, 5, 18))
        self.assertEqual(fe, date(2026, 6, 10))


class TestBuildManifest(unittest.TestCase):
    # window이 A75605(seg [2026-04-20, 2026-05-18)) + A75606(seg [2026-05-18, 2026-06-15))를 덮음
    WS = date(2026, 4, 25)
    WE = date(2026, 6, 10)

    def _seq(self):
        seq = B.build_contract_sequence(self.WS, self.WE)
        # sanity: 첫/끝 contract 확인 (테스트 전제)
        self.assertEqual(seq[0][0].short_code, "A75605")
        self.assertEqual(seq[-1][0].short_code, "A75606")
        return seq

    def test_normal_no_hard_issues_sorted(self):
        seq = self._seq()
        fetch = _fetch_from({
            "A75605": [_row(date(2026, 4, 25), "A75605"), _row(date(2026, 5, 15), "A75605")],
            "A75606": [_row(date(2026, 5, 18), "A75606"), _row(date(2026, 6, 10), "A75606")],
        })
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertEqual(res.hard_issues, [])
        self.assertEqual(
            [r["date_kst"] for r in res.rows],
            [date(2026, 4, 25), date(2026, 5, 15), date(2026, 5, 18), date(2026, 6, 10)],
        )

    def test_expiry_row_in_previous_segment_fails(self):
        # 만기일 2026-05-18을 C0(A75605, seg_end=2026-05-18) fetch에 → boundary hard fail
        seq = self._seq()
        fetch = _fetch_from({"A75605": [_row(date(2026, 5, 18), "A75605")]})
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertTrue(any("boundary" in i for i in res.hard_issues))
        self.assertEqual(res.rows, [])

    def test_expiry_row_in_next_segment_ok(self):
        seq = self._seq()
        fetch = _fetch_from({"A75606": [_row(date(2026, 5, 18), "A75606")]})
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertEqual(res.hard_issues, [])
        self.assertIn(date(2026, 5, 18), [r["date_kst"] for r in res.rows])

    def test_row_outside_segment_fails(self):
        # C0 fetch에 C1 segment 날짜(2026-06-01 > seg_end) → segment 밖 hard
        seq = self._seq()
        fetch = _fetch_from({"A75605": [_row(date(2026, 6, 1), "A75605")]})
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertTrue(any("밖" in i for i in res.hard_issues))

    def test_row_outside_window_fails(self):
        # C0 segment 안이지만 window_start 전 (2026-04-22 ∈ seg, < ws 2026-04-25)
        seq = self._seq()
        fetch = _fetch_from({"A75605": [_row(date(2026, 4, 22), "A75605")]})
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertTrue(any("window" in i for i in res.hard_issues))

    def test_duplicate_date_fails(self):
        seq = self._seq()
        fetch = _fetch_from({"A75605": [_row(date(2026, 5, 15), "A75605"),
                                        _row(date(2026, 5, 15), "A75605")]})
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertTrue(any("duplicate" in i for i in res.hard_issues))

    def test_gap_surface_warning_not_hard(self):
        seq = self._seq()
        fetch = _fetch_from({
            "A75605": [_row(date(2026, 4, 25), "A75605"), _row(date(2026, 5, 15), "A75605")],
        })
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertEqual(res.hard_issues, [])
        self.assertTrue(any("gap" in w for w in res.warnings))  # 20일 gap >= 4

    def test_cap_surface_warning(self):
        seq = self._seq()
        many = [_row(date(2026, 5, 15), "A75605")] * B.KIS_DAILY_ROWS_CAP  # 100 rows (동일 날짜)
        fetch = _fetch_from({"A75605": many})
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertTrue(any("cap" in w for w in res.cap_warnings))  # cap은 cap_warnings로 분리
        # cap은 build_manifest 단계 surface (hard 아님) — write-intended promote에서만 hard
        self.assertFalse(any("cap" in i for i in res.hard_issues))
        self.assertFalse(any("cap" in w for w in res.warnings))  # warnings(gap)엔 cap 없음

    def test_contract_code_mismatch_fails(self):
        # fetch_fn이 A75605 fetch에 contract_code=A75606 row를 줌 (wrapper/mock 실수 방어).
        # date_kst는 A75605 segment 안이지만 contract_code 불일치 → (date→contract) invariant hard fail.
        seq = self._seq()
        fetch = _fetch_from({"A75605": [_row(date(2026, 5, 15), "A75606")]})
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertTrue(any("mismatch" in i for i in res.hard_issues))
        self.assertEqual(res.rows, [])  # mismatch row는 manifest에 안 들어감

    def test_holiday_boundary_mismatch_ok(self):
        # window_start 2026-04-25지만 첫 row 2026-04-27 (경계 불일치) → 정상 (hard 0)
        seq = self._seq()
        fetch = _fetch_from({"A75605": [_row(date(2026, 4, 27), "A75605")]})
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertEqual(res.hard_issues, [])

    def test_boundary_samples_collected(self):
        # 만기 전일(seg_end - 1 = 2026-05-17)은 boundary sample
        seq = self._seq()
        fetch = _fetch_from({"A75605": [_row(date(2026, 5, 17), "A75605")]})
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertTrue(any(s["date_kst"] == date(2026, 5, 17) for s in res.boundary_samples))


class TestPromoteWriteIntended(unittest.TestCase):
    """단위 4 — cap_warnings를 write-intended path에서 hard로 승격. gap은 surface 유지."""
    WS = date(2026, 4, 25)
    WE = date(2026, 6, 10)

    def _seq(self):
        return B.build_contract_sequence(self.WS, self.WE)

    def _cap_result(self):
        many = [_row(date(2026, 5, 15), "A75605")] * B.KIS_DAILY_ROWS_CAP
        return B.build_manifest(self._seq(), self.WS, self.WE, _fetch_from({"A75605": many}))

    def test_cap_surface_before_promote(self):
        res = self._cap_result()
        self.assertTrue(res.cap_warnings)
        self.assertFalse(any("cap" in i for i in res.hard_issues))  # promote 전 hard 아님

    def test_cap_hard_after_promote(self):
        promoted = B.promote_write_intended_manifest_warnings(self._cap_result())
        self.assertTrue(any("cap 격상" in i for i in promoted.hard_issues))  # promote 후 hard
        self.assertTrue(promoted.cap_warnings)  # audit 유지

    def test_gap_not_promoted(self):
        seq = self._seq()
        fetch = _fetch_from({
            "A75605": [_row(date(2026, 4, 25), "A75605"), _row(date(2026, 5, 15), "A75605")],
        })
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertTrue(any("gap" in w for w in res.warnings))
        promoted = B.promote_write_intended_manifest_warnings(res)
        self.assertEqual(promoted.warnings, res.warnings)            # gap 유지
        self.assertFalse(any("gap" in i for i in promoted.hard_issues))  # gap hard 아님

    def test_existing_hard_preserved(self):
        # cap + segment 밖 row(hard) 동시 → promote 후 기존 hard 보존 + cap 추가
        many = [_row(date(2026, 5, 15), "A75605")] * B.KIS_DAILY_ROWS_CAP
        many.append(_row(date(2026, 6, 1), "A75605"))  # segment 밖 → hard
        res = B.build_manifest(self._seq(), self.WS, self.WE, _fetch_from({"A75605": many}))
        pre_hard = list(res.hard_issues)
        self.assertTrue(pre_hard)
        promoted = B.promote_write_intended_manifest_warnings(res)
        for h in pre_hard:
            self.assertIn(h, promoted.hard_issues)  # 보존

    def test_no_cap_returns_same(self):
        seq = self._seq()
        fetch = _fetch_from({"A75605": [_row(date(2026, 5, 15), "A75605")]})
        res = B.build_manifest(seq, self.WS, self.WE, fetch)
        self.assertEqual(res.cap_warnings, [])
        promoted = B.promote_write_intended_manifest_warnings(res)
        self.assertIs(promoted, res)  # cap 없으면 no-op (동일 객체)


if __name__ == "__main__":
    unittest.main()
