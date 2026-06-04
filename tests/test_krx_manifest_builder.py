"""Step 4B (KRX source 전환) 단위 — date-based manifest builder 테스트.

network 0 (fetch_fn mock) + duck-typed SimpleNamespace contract. weekday loop + BAS_DD 가드 중심:
  - 정상 거래일 → 변환 row
  - 주말 → 미호출 + row 없음
  - 휴장 empty → missing(no_front_month)
  - stale BAS_DD mismatch → missing(bas_dd_mismatch) + **manifest row 미생성** (Codex 핵심)
  - 변환 실패(high/low 빈) → hard
  - 만기일(seg_end) 제외 = boundary=next
  - 0 rows → hard / gap surface / duplicate 방어
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_krx_openapi_source_daily_rates as K  # noqa: E402

# seg [2026-05-18, 2026-06-15) — 2026-06-15(Mon) = 만기일(seg_end, 제외)
_C = SimpleNamespace(short_code="A75606", contract_month="202606", expiry_date=date(2026, 6, 15))
_SEQ = [(_C, date(2026, 5, 18), date(2026, 6, 15))]
_YMD = lambda d: d.strftime("%Y%m%d")  # noqa: E731


def _raw(bas_dd, contract_month="202606", cls="1496.5", hi="1531.1", lo="1521.9", opn="1517.6", isu=None):
    isu = isu if isu is not None else f"미국달러 F {contract_month} (주간)"
    return {
        "BAS_DD": bas_dd, "PROD_NM": "미국달러 선물", "MKT_NM": "정규",
        "ISU_CD": "A75606", "ISU_NM": isu,
        "TDD_CLSPRC": cls, "TDD_HGPRC": hi, "TDD_LWPRC": lo, "TDD_OPNPRC": opn,
        "SETL_PRC": cls, "SPOT_PRC": "1495.0", "ACC_TRDVOL": "12345", "ACC_OPNINT_QTY": "678",
    }


class _Fetch:
    """fetch_fn(date) mock — 호출 날짜 기록 (주말 미호출 검증용)."""
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, d):
        self.calls.append(d)
        return list(self.mapping.get(d, []))


def _day_rows(rows):
    return [r["date_kst"] for r in rows]


class TestBuildKrxManifest(unittest.TestCase):
    # 평일: 06-08 Mon ~ 06-12 Fri / 주말: 06-13 Sat, 06-14 Sun
    def test_normal_weekdays_produce_rows(self):
        ws, we = date(2026, 6, 8), date(2026, 6, 12)
        days = [date(2026, 6, d) for d in (8, 9, 10, 11, 12)]
        fetch = _Fetch({d: [_raw(_YMD(d))] for d in days})
        res = K.build_krx_manifest(_SEQ, ws, we, fetch)
        self.assertEqual(res.hard_issues, [])
        self.assertEqual(res.missing_dates, [])
        self.assertEqual(_day_rows(res.rows), days)
        # 변환 wiring 확인 (close/rate invariant + date_kst)
        self.assertEqual(res.rows[0]["close"], Decimal("1496.5"))
        self.assertEqual(res.rows[0]["rate"], res.rows[0]["close"])
        self.assertEqual(res.rows[0]["source_method"], "krx_openapi_daily")

    def test_weekend_not_called_not_in_rows(self):
        # window이 주말(06-13/14)을 포함해도 호출 안 함 + row 없음. 06-15(만기)도 seg_end 제외.
        ws, we = date(2026, 6, 8), date(2026, 6, 16)
        weekdays = [date(2026, 6, d) for d in (8, 9, 10, 11, 12)]  # 06-14까지 중 평일
        fetch = _Fetch({d: [_raw(_YMD(d))] for d in weekdays})
        res = K.build_krx_manifest(_SEQ, ws, we, fetch)
        self.assertEqual(_day_rows(res.rows), weekdays)
        # 주말 미호출
        self.assertNotIn(date(2026, 6, 13), fetch.calls)
        self.assertNotIn(date(2026, 6, 14), fetch.calls)
        # 만기일(seg_end=06-15)·그 이후(06-16) 미호출 (boundary=next)
        self.assertNotIn(date(2026, 6, 15), fetch.calls)
        self.assertNotIn(date(2026, 6, 16), fetch.calls)

    def test_holiday_empty_missing(self):
        ws, we = date(2026, 6, 8), date(2026, 6, 12)
        days = [date(2026, 6, d) for d in (8, 9, 10, 11, 12)]
        mapping = {d: [_raw(_YMD(d))] for d in days}
        mapping[date(2026, 6, 10)] = []  # 휴장 — 빈 응답
        fetch = _Fetch(mapping)
        res = K.build_krx_manifest(_SEQ, ws, we, fetch)
        self.assertEqual(res.hard_issues, [])
        self.assertNotIn(date(2026, 6, 10), _day_rows(res.rows))
        self.assertTrue(any(m["date"] == date(2026, 6, 10) and m["reason"] == "no_front_month"
                            for m in res.missing_dates))

    def test_stale_bas_dd_mismatch_no_manifest_row(self):
        # Codex 핵심: stale 응답은 surface하되 manifest row 절대 생성 금지.
        # stale BAS_DD를 colliding 안 하는 far-out 날짜로 줘서 "응답 날짜가 row로 안 샘"을 명확히 검증.
        ws, we = date(2026, 6, 8), date(2026, 6, 12)
        days = [date(2026, 6, d) for d in (8, 9, 10, 11, 12)]
        mapping = {d: [_raw(_YMD(d))] for d in days}
        mapping[date(2026, 6, 10)] = [_raw("20260101")]  # 요청 06-10인데 응답 BAS_DD=2026-01-01 (stale)
        fetch = _Fetch(mapping)
        res = K.build_krx_manifest(_SEQ, ws, we, fetch)
        self.assertEqual(res.hard_issues, [])
        # 요청 날짜(06-10) 미생성 + 응답의 stale 날짜(01-01)도 manifest에 침입 안 함
        self.assertNotIn(date(2026, 6, 10), _day_rows(res.rows))
        self.assertNotIn(date(2026, 1, 1), _day_rows(res.rows))
        self.assertEqual(_day_rows(res.rows), [date(2026, 6, d) for d in (8, 9, 11, 12)])
        self.assertTrue(any(m["date"] == date(2026, 6, 10)
                            and m["reason"].startswith("bas_dd_mismatch")
                            for m in res.missing_dates))

    def test_conversion_failure_hard(self):
        # present row인데 high 빈 → source_ohlc 불완전 → coverage hard, row 미생성
        ws, we = date(2026, 6, 8), date(2026, 6, 12)
        days = [date(2026, 6, d) for d in (8, 9, 10, 11, 12)]
        mapping = {d: [_raw(_YMD(d))] for d in days}
        mapping[date(2026, 6, 11)] = [_raw("20260611", hi="")]  # high 빈
        fetch = _Fetch(mapping)
        res = K.build_krx_manifest(_SEQ, ws, we, fetch)
        self.assertTrue(any("변환 실패(coverage)" in h for h in res.hard_issues))
        self.assertNotIn(date(2026, 6, 11), _day_rows(res.rows))

    def test_malformed_decimal_hard(self):
        # CLS 비어있진 않으나 숫자 아님("abc") → _parse_krx_decimal InvalidOperation → coverage hard
        # (InvalidOperation은 ValueError 서브클래스 아님 — except 누락 시 propagate; 회귀 잠금)
        ws, we = date(2026, 6, 8), date(2026, 6, 12)
        days = [date(2026, 6, d) for d in (8, 9, 10, 11, 12)]
        mapping = {d: [_raw(_YMD(d))] for d in days}
        mapping[date(2026, 6, 9)] = [_raw("20260609", cls="abc")]  # 숫자 파싱 불가
        fetch = _Fetch(mapping)
        res = K.build_krx_manifest(_SEQ, ws, we, fetch)
        self.assertTrue(any("변환 실패(coverage)" in h for h in res.hard_issues))
        self.assertNotIn(date(2026, 6, 9), _day_rows(res.rows))

    def test_boundary_expiry_excluded(self):
        # 만기일(06-15)에 row가 와도 seg_end 제외라 미호출·미포함
        ws, we = date(2026, 6, 8), date(2026, 6, 16)
        fetch = _Fetch({date(2026, 6, 15): [_raw("20260615")]})  # 만기일만 제공
        res = K.build_krx_manifest(_SEQ, ws, we, fetch)
        self.assertNotIn(date(2026, 6, 15), fetch.calls)
        self.assertEqual(res.rows, [])  # 평일 제공 0 → 0 rows hard
        self.assertTrue(any("0 rows" in h for h in res.hard_issues))

    def test_boundary_samples_near_segment_start(self):
        # seg_start가 평일이면 그 ±1일이 boundary sample (mock seg로 검증)
        c = SimpleNamespace(short_code="A75606", contract_month="202606", expiry_date=date(2026, 6, 19))
        seq = [(c, date(2026, 6, 8), date(2026, 6, 19))]  # seg_start=06-08(Mon)
        ws, we = date(2026, 6, 8), date(2026, 6, 12)
        days = [date(2026, 6, d) for d in (8, 9, 10, 11, 12)]
        fetch = _Fetch({d: [_raw(_YMD(d))] for d in days})
        res = K.build_krx_manifest(seq, ws, we, fetch)
        bs_dates = [b["date_kst"] for b in res.boundary_samples]
        self.assertIn(date(2026, 6, 8), bs_dates)   # seg_start (d-seg_start=0)
        self.assertIn(date(2026, 6, 9), bs_dates)   # seg_start+1
        self.assertNotIn(date(2026, 6, 11), bs_dates)  # 경계에서 멂

    def test_zero_rows_hard(self):
        ws, we = date(2026, 6, 8), date(2026, 6, 12)
        fetch = _Fetch({})  # 전 평일 빈 응답
        res = K.build_krx_manifest(_SEQ, ws, we, fetch)
        self.assertEqual(res.rows, [])
        self.assertTrue(any("0 rows" in h for h in res.hard_issues))
        self.assertEqual(len(res.missing_dates), 5)  # 5 평일 모두 missing

    def test_gap_surface_warning(self):
        # 06-08, 06-12만 row (gap 4d >= GAP_SURFACE_THRESHOLD_DAYS) → warning, hard 아님
        ws, we = date(2026, 6, 8), date(2026, 6, 12)
        mapping = {date(2026, 6, 8): [_raw("20260608")], date(2026, 6, 12): [_raw("20260612")]}
        fetch = _Fetch(mapping)
        res = K.build_krx_manifest(_SEQ, ws, we, fetch)
        self.assertEqual(res.hard_issues, [])
        self.assertEqual(_day_rows(res.rows), [date(2026, 6, 8), date(2026, 6, 12)])
        self.assertTrue(any("gap 4d" in w for w in res.warnings))

    def test_select_duplicate_in_day_hard(self):
        # 같은 날 동일 contract_month 유효 row 2개 → select raise → hard
        ws, we = date(2026, 6, 8), date(2026, 6, 8)
        dup = [_raw("20260608", cls="1496.5"), _raw("20260608", cls="1496.6")]
        fetch = _Fetch({date(2026, 6, 8): dup})
        res = K.build_krx_manifest(_SEQ, ws, we, fetch)
        self.assertTrue(any("select 이상" in h for h in res.hard_issues))
        self.assertEqual(res.rows, [])


class TestDuplicateDateIssues(unittest.TestCase):
    def test_no_duplicate(self):
        rows = [{"date_kst": date(2026, 6, 8)}, {"date_kst": date(2026, 6, 9)}]
        self.assertEqual(K._duplicate_date_issues(rows), [])

    def test_duplicate_surfaced(self):
        rows = [{"date_kst": date(2026, 6, 8)}, {"date_kst": date(2026, 6, 8)}]
        issues = K._duplicate_date_issues(rows)
        self.assertTrue(any("duplicate date_kst" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
