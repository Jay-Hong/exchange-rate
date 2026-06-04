"""Step 4B (KRX source 전환) 단위 — KRX fut_bydd_trd parser/filter 테스트.

mock response (POC 2026-05-18 구조 기반, 외부 의존 0).
필터: SP 제외 / MKT=정규 / selected YYYYMM / CLS non-empty / 만기일=next / 중복 hard.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_krx_openapi_source_daily_rates as K  # noqa: E402


def _row(prod, mkt, isu, cls, setl=""):
    return {"BAS_DD": "20260518", "PROD_NM": prod, "MKT_NM": mkt,
            "ISU_NM": isu, "TDD_CLSPRC": cls, "SETL_PRC": setl}


# 2026-05-18(A75605 만기일) 구조: 202605(만기월물) + 202606(next) 공존, 원월물 CLS 빈, SP, 타상품
SAMPLE = {"OutBlock_1": [
    _row("미국달러 선물", "정규", "미국달러 F 202605 (주간)", "1505.80", "1505.80"),  # 만기월물
    _row("미국달러 선물", "정규", "미국달러 F 202606 (주간)", "1496.50", "1496.50"),  # next(front)
    _row("미국달러 선물", "정규", "미국달러 F 202607 (주간)", "1496.70", "1495.00"),
    _row("미국달러 선물", "정규", "미국달러 F 202609 (주간)", "", "1492.40"),          # 원월물 CLS 빈
    _row("미국달러 선물", "정규", "미국달러 SP 2605-2606 (주간)", "-1.20", ""),         # 스프레드
    _row("코스피200 선물", "정규", "코스피200 F 202606 (주간)", "1409.95", "1409.95"),  # 타상품
]}


class TestParse(unittest.TestCase):
    def test_parse_ok(self):
        self.assertEqual(len(K.parse_krx_fut_response(SAMPLE)), 6)

    def test_parse_missing_outblock(self):
        with self.assertRaises(ValueError):
            K.parse_krx_fut_response({})

    def test_parse_wrong_type(self):
        with self.assertRaises(ValueError):
            K.parse_krx_fut_response({"OutBlock_1": "not a list"})


class TestSelectFrontMonth(unittest.TestCase):
    def setUp(self):
        self.rows = K.parse_krx_fut_response(SAMPLE)

    def test_select_next_at_expiry_boundary(self):
        # 만기일=next: contract_month=202606 → 만기월물 202605 무시, next 202606 선택
        r = K.select_usd_front_month_row(self.rows, "202606")
        self.assertIsNotNone(r)
        self.assertEqual(r["TDD_CLSPRC"], "1496.50")  # KIS A75606과 일치값
        self.assertIn("202606", r["ISU_NM"])

    def test_select_expiry_month_when_chosen(self):
        # 만기월물도 sequence가 지정하면 선택 가능 (단 우리 rule은 next)
        r = K.select_usd_front_month_row(self.rows, "202605")
        self.assertEqual(r["TDD_CLSPRC"], "1505.80")

    def test_sp_spread_excluded(self):
        # SP 스프레드는 startswith "미국달러 F " 아니라 제외 — 202606 선택이 F 단일물
        r = K.select_usd_front_month_row(self.rows, "202606")
        self.assertTrue(r["ISU_NM"].startswith("미국달러 F "))
        self.assertNotIn("SP", r["ISU_NM"])

    def test_other_product_excluded(self):
        # 코스피200 F 202606은 PROD_NM 불일치라 제외 (미국달러선물만)
        r = K.select_usd_front_month_row(self.rows, "202606")
        self.assertEqual(r["PROD_NM"], "미국달러 선물")

    def test_empty_close_returns_none(self):
        # 원월물(202609)은 CLS 빈 → None (호출자 coverage hard)
        self.assertIsNone(K.select_usd_front_month_row(self.rows, "202609"))

    def test_absent_month_returns_none(self):
        # 응답에 없는 월물 → None
        self.assertIsNone(K.select_usd_front_month_row(self.rows, "203012"))

    def test_night_session_excluded(self):
        # MKT_NM != 정규 (야간) → 제외
        rows = [_row("미국달러 선물", "야간", "미국달러 F 202606 (주간)", "1496.50")]
        self.assertIsNone(K.select_usd_front_month_row(rows, "202606"))

    def test_duplicate_valid_rows_hard(self):
        # 동일 contract_month 유효 row 2개 → raise (데이터/필터 이상)
        rows = [
            _row("미국달러 선물", "정규", "미국달러 F 202606 (주간)", "1496.50"),
            _row("미국달러 선물", "정규", "미국달러 F 202606 (주간)", "1496.60"),
        ]
        with self.assertRaises(ValueError):
            K.select_usd_front_month_row(rows, "202606")

    def test_prefix_not_oversharing(self):
        # "미국달러 F 202606"이 "미국달러 F 2026..." 다른 월물을 잘못 매칭하지 않음
        r = K.select_usd_front_month_row(self.rows, "202606")
        self.assertNotIn("202607", r["ISU_NM"])
        self.assertNotIn("202605", r["ISU_NM"])

    def test_suffix_variant_excluded(self):
        # exact 매칭: "미국달러 F 202606X (주간)" 같은 suffix 변형은 제외 (startswith 느슨함 차단)
        rows = [_row("미국달러 선물", "정규", "미국달러 F 202606X (주간)", "1496.50")]
        self.assertIsNone(K.select_usd_front_month_row(rows, "202606"))

    def test_isu_night_suffix_excluded(self):
        # ISU_NM이 (야간)이면 정규식 `\(주간\)$` 불일치로 제외 (MKT 필터와 독립적으로도)
        rows = [_row("미국달러 선물", "정규", "미국달러 F 202606 (야간)", "1496.50")]
        self.assertIsNone(K.select_usd_front_month_row(rows, "202606"))

    def test_trailing_text_excluded(self):
        # "(주간)" 뒤 추가 텍스트는 anchor `$`로 차단
        rows = [_row("미국달러 선물", "정규", "미국달러 F 202606 (주간) X", "1496.50")]
        self.assertIsNone(K.select_usd_front_month_row(rows, "202606"))


if __name__ == "__main__":
    unittest.main()
