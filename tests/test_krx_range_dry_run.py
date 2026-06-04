"""Step 4B (KRX 전환) 단위 — run_krx_range_dry_run 게이트 단위 테스트.

network 0 (fetch_fn(date) mock) + in-memory SQLite (compare). exit code/sentinel 분기 중심:
  - 정상 + 빈 DB → rows_to_write, PASS
  - 전수 transitional(manifest krx vs DB kis) → PASS_WITH_TRANSITIONAL
  - manifest hard(변환실패/0rows) → compare skip + FAIL
  - compare hard(orphan) → FAIL
  - fetch 예외 → FETCH_ERROR + FAIL
  - missing_dates JSON surface / MANIFEST_DATE_MIN/MAX None-safe
DB write 0 — _emit_compare_and_gate의 query SELECT만.
"""
from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_krx_openapi_source_daily_rates as K  # noqa: E402

_YMD = lambda d: d.strftime("%Y%m%d")  # noqa: E731


def _raw(bas_dd, contract_month="202606", cls="1496.5", hi="1531.1", lo="1521.9", opn="1517.6"):
    return {
        "BAS_DD": bas_dd, "PROD_NM": "미국달러 선물", "MKT_NM": "정규",
        "ISU_CD": "A75606", "ISU_NM": f"미국달러 F {contract_month} (주간)",
        "TDD_CLSPRC": cls, "TDD_HGPRC": hi, "TDD_LWPRC": lo, "TDD_OPNPRC": opn,
        "SETL_PRC": cls, "SPOT_PRC": "1495.0", "ACC_TRDVOL": "12345", "ACC_OPNINT_QTY": "678",
    }


class TestRunKrxRangeDryRun(unittest.TestCase):
    # window 06-08(Mon)~06-12(Fri): build_contract_sequence → 단일 A75606/202606 seg [05-18, 06-15)
    WS = date(2026, 6, 8)
    WE = date(2026, 6, 12)
    WEEKDAYS = [date(2026, 6, d) for d in (8, 9, 10, 11, 12)]

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import SourceDailyRate
        self.SourceDailyRate = SourceDailyRate
        engine = create_engine("sqlite:///:memory:")
        SourceDailyRate.__table__.create(engine)
        self.db = sessionmaker(bind=engine)()

    def tearDown(self):
        self.db.close()

    def _insert(self, d, source_method="kis_daily_backfill", close="1496.5"):
        # manifest row(krx_openapi_daily)와 값 일치 → source_method만 다르면 transitional
        self.db.add(self.SourceDailyRate(
            source="krx", asset="usd-krw-futures", date_kst=d,
            rate=Decimal(close), high=Decimal("1531.1"), low=Decimal("1521.9"), close=Decimal(close),
            ohlc_quality="source_ohlc", close_basis="krx_cf_close_1545",
            source_method=source_method, contract_code="A75606",
            basis_date=None, published_at=None,
            metadata_json={"contract_short_code": "A75606", "contract_month": "202606",
                           "contract_expiry_date": "2026-06-15", "open": "1517.6"},
        ))
        self.db.commit()

    def _fetch(self, mapping):
        def fetch_fn(d):
            return list(mapping.get(d, []))
        return fetch_fn

    def _run(self, fetch):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = K.run_krx_range_dry_run(self.WS, self.WE, fetch, self.db)
        return rc, buf.getvalue()

    def test_pass_clean_empty_db(self):
        fetch = self._fetch({d: [_raw(_YMD(d))] for d in self.WEEKDAYS})
        rc, out = self._run(fetch)
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"(?m)^RANGE_DRY_RUN_RESULT=PASS$")  # exact-line
        self.assertIn("MANIFEST_ROW_COUNT=5", out)
        self.assertIn("ROWS_TO_WRITE_COUNT=5", out)
        self.assertIn("MANIFEST_DATE_MIN=2026-06-08", out)
        self.assertIn("MANIFEST_DATE_MAX=2026-06-12", out)
        self.assertIn("COMPARE_STATUS=ran", out)

    def test_transitional_pass_with_transitional(self):
        fetch = self._fetch({d: [_raw(_YMD(d))] for d in self.WEEKDAYS})
        for d in self.WEEKDAYS:
            self._insert(d, source_method="kis_daily_backfill")  # 기존 KIS seed
        rc, out = self._run(fetch)
        self.assertEqual(rc, 0)
        self.assertIn("RANGE_DRY_RUN_RESULT=PASS_WITH_TRANSITIONAL", out)
        self.assertIn("TRANSITIONAL_SOURCE_METHOD_MATCH_COUNT=5", out)
        self.assertIn("MATCHED_EXISTING_COUNT=0", out)
        self.assertIn("ROWS_TO_WRITE_COUNT=0", out)

    def test_manifest_conversion_fail_skips_compare(self):
        mapping = {d: [_raw(_YMD(d))] for d in self.WEEKDAYS}
        mapping[date(2026, 6, 10)] = [_raw("20260610", hi="")]  # high 빈 → 변환 실패
        fetch = self._fetch(mapping)
        rc, out = self._run(fetch)
        self.assertEqual(rc, 1)
        self.assertIn("COMPARE_STATUS=skipped_due_to_manifest_hard", out)
        self.assertIn("RANGE_DRY_RUN_RESULT=FAIL", out)

    def test_manifest_zero_rows_skips_compare(self):
        fetch = self._fetch({})  # 전 평일 빈 → 0 rows hard
        rc, out = self._run(fetch)
        self.assertEqual(rc, 1)
        self.assertIn("MANIFEST_ROW_COUNT=0", out)
        self.assertIn("MANIFEST_DATE_MIN=None", out)   # rows 0 None-safe
        self.assertIn("MANIFEST_DATE_MAX=None", out)
        self.assertIn("COMPARE_STATUS=skipped_due_to_manifest_hard", out)
        self.assertIn("RANGE_DRY_RUN_RESULT=FAIL", out)

    def test_compare_orphan_fail(self):
        # 06-10 manifest 누락(missing) + DB에 06-10 존재 → ④ orphan hard
        mapping = {d: [_raw(_YMD(d))] for d in self.WEEKDAYS if d != date(2026, 6, 10)}
        fetch = self._fetch(mapping)
        self._insert(date(2026, 6, 10), source_method="kis_daily_backfill")
        rc, out = self._run(fetch)
        self.assertEqual(rc, 1)
        self.assertIn("COMPARE_STATUS=ran", out)
        self.assertIn("RANGE_DRY_RUN_RESULT=FAIL", out)

    def test_missing_dates_json_surface(self):
        # 06-10 빈 응답 → missing(no_front_month), 나머지 정상 + 빈 DB → PASS
        mapping = {d: [_raw(_YMD(d))] for d in self.WEEKDAYS if d != date(2026, 6, 10)}
        fetch = self._fetch(mapping)
        rc, out = self._run(fetch)
        self.assertEqual(rc, 0)
        self.assertIn("MISSING_DATES_COUNT=1", out)
        self.assertIn('"date": "2026-06-10"', out)
        self.assertIn('"reason": "no_front_month"', out)

    def test_fetch_exception_fails_gracefully(self):
        def bad_fetch(d):
            raise RuntimeError("KRX down")
        rc, out = self._run(bad_fetch)
        self.assertEqual(rc, 1)
        self.assertIn("FETCH_ERROR", out)
        self.assertIn("RANGE_DRY_RUN_RESULT=FAIL", out)


class TestRunKrxRangeDryRunMainConfig(unittest.TestCase):
    """main wiring config-fail 분기 (env 미설정 / one-sided / inverted window) — network·DB 0."""

    def _args(self, start=None, end=None, min_interval=1.0):
        return SimpleNamespace(start_date=start, end_date=end, min_interval_sec=min_interval)

    def test_missing_env_returns_1(self):
        # KRX_OPENAPI_AUTH_KEY 미설정 → exit 1 (HTTP/DB 진입 전 fail-close)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(K.KRX_OPENAPI_AUTH_KEY_ENV, None)
            with redirect_stdout(buf):
                rc = K._run_krx_range_dry_run_main(self._args(date(2026, 6, 8), date(2026, 6, 12)))
        self.assertEqual(rc, 1)
        self.assertIn(K.KRX_OPENAPI_AUTH_KEY_ENV, buf.getvalue())

    def test_one_sided_window_returns_2(self):
        # env 있으나 --start만 → window None → exit 2 (make_krx_fetch_fn/DB 진입 전)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {K.KRX_OPENAPI_AUTH_KEY_ENV: "dummy"}, clear=False):
            with redirect_stdout(buf):
                rc = K._run_krx_range_dry_run_main(self._args(date(2026, 6, 8), None))
        self.assertEqual(rc, 2)

    def test_inverted_window_returns_2(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {K.KRX_OPENAPI_AUTH_KEY_ENV: "dummy"}, clear=False):
            with redirect_stdout(buf):
                rc = K._run_krx_range_dry_run_main(self._args(date(2026, 6, 12), date(2026, 6, 8)))
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
