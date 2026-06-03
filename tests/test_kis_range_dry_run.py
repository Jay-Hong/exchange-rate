"""Step 4B 단위 5 — run_range_dry_run write-intended 게이트 단위 테스트.

network 0 (fetch_fn mock) + in-memory SQLite (compare). exit code 분기 중심:
  - 정상 → exit 0 (rows_to_write 산출)
  - manifest hard(boundary/cap promote) → compare skip + exit 1 (COMPARE_STATUS=skipped)
  - compare hard(④ orphan) → exit 1
  - matched_existing → skip, exit 0
DB write 0 — query_existing SELECT만.
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_kis_source_daily_rates as B  # noqa: E402


class TestRunRangeDryRun(unittest.TestCase):
    WS = date(2026, 4, 25)   # A75605 seg[2026-04-20,2026-05-18) + A75606 seg[2026-05-18,2026-06-15)
    WE = date(2026, 6, 10)

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

    def _insert(self, d, close="1496.5", contract_code="A75606"):
        self.db.add(self.SourceDailyRate(
            source="krx", asset="usd-krw-futures", date_kst=d,
            rate=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close),
            ohlc_quality="source_ohlc", close_basis="krx_cf_close_1545",
            source_method="kis_daily_backfill", contract_code=contract_code,
            basis_date=None, published_at=None,
            metadata_json={"contract_short_code": contract_code, "contract_month": "202606",
                           "contract_expiry_date": "2026-06-15", "open": close},
        ))
        self.db.commit()

    def _krx_row(self, d, contract_code, close="1496.5"):
        return {
            "source": "krx", "asset": "usd-krw-futures", "date_kst": d,
            "rate": Decimal(close), "high": Decimal(close), "low": Decimal(close), "close": Decimal(close),
            "ohlc_quality": "source_ohlc", "close_basis": "krx_cf_close_1545",
            "source_method": "kis_daily_backfill", "contract_code": contract_code,
            "basis_date": None, "published_at": None,
            "metadata_json": {"contract_short_code": contract_code, "contract_month": "202606",
                              "contract_expiry_date": "2026-06-15", "open": close},
        }

    def _fetch(self, mapping):
        def fetch_fn(contract, fs, fe):
            return list(mapping.get(contract.short_code, []))
        return fetch_fn

    def _run(self, fetch):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = B.run_range_dry_run(self.WS, self.WE, fetch, self.db)
        return rc, buf.getvalue()

    def test_pass_clean_manifest_empty_db(self):
        # manifest 정상 + DB 비어있음 → 전부 rows_to_write, exit 0
        fetch = self._fetch({
            "A75605": [self._krx_row(date(2026, 5, 15), "A75605")],
            "A75606": [self._krx_row(date(2026, 5, 20), "A75606")],
        })
        rc, out = self._run(fetch)
        self.assertEqual(rc, 0)
        self.assertIn("RANGE_DRY_RUN_RESULT=PASS", out)
        self.assertIn("ROWS_TO_WRITE_COUNT=2", out)
        self.assertIn("COMPARE_STATUS=ran", out)

    def test_manifest_hard_skips_compare(self):
        # 만기일 2026-05-18을 previous(A75605, seg_end) fetch에 → boundary hard → compare skip
        fetch = self._fetch({"A75605": [self._krx_row(date(2026, 5, 18), "A75605")]})
        self._insert(date(2026, 5, 25), contract_code="A75606")  # orphan 심어도 skip이라 미검출
        rc, out = self._run(fetch)
        self.assertEqual(rc, 1)
        self.assertIn("COMPARE_STATUS=skipped_due_to_manifest_hard", out)
        self.assertIn("RANGE_DRY_RUN_RESULT=FAIL", out)

    def test_cap_promote_hard_skips_compare(self):
        many = [self._krx_row(date(2026, 5, 15), "A75605")] * B.KIS_DAILY_ROWS_CAP
        fetch = self._fetch({"A75605": many})
        rc, out = self._run(fetch)
        self.assertEqual(rc, 1)
        self.assertIn("COMPARE_STATUS=skipped_due_to_manifest_hard", out)

    def test_compare_orphan_hard_exit1(self):
        # manifest 정상이지만 DB(window 안)에 manifest 없는 row → ④ orphan hard
        fetch = self._fetch({"A75605": [self._krx_row(date(2026, 5, 15), "A75605")]})
        self._insert(date(2026, 5, 20), contract_code="A75606")  # manifest엔 없음
        rc, out = self._run(fetch)
        self.assertEqual(rc, 1)
        self.assertIn("COMPARE_STATUS=ran", out)
        self.assertIn("RANGE_DRY_RUN_RESULT=FAIL", out)

    def test_matched_existing_skip_exit0(self):
        # manifest = DB 일치 → matched, rows_to_write 0, exit 0
        d = date(2026, 5, 15)
        fetch = self._fetch({"A75605": [self._krx_row(d, "A75605", close="1496.5")]})
        self._insert(d, close="1496.5", contract_code="A75605")
        rc, out = self._run(fetch)
        self.assertEqual(rc, 0)
        self.assertIn("ROWS_TO_WRITE_COUNT=0", out)
        self.assertIn("MATCHED_EXISTING_COUNT=1", out)

    def test_fetch_exception_fails_gracefully(self):
        # fetch_fn 예외 → traceback 대신 FETCH_ERROR + exit 1 (gate 일관성)
        def bad_fetch(contract, fs, fe):
            raise RuntimeError("KIS down")
        rc, out = self._run(bad_fetch)
        self.assertEqual(rc, 1)
        self.assertIn("FETCH_ERROR", out)
        self.assertIn("RANGE_DRY_RUN_RESULT=FAIL", out)


class TestResolveWindow(unittest.TestCase):
    """--start/--end 고정 range 경로 (Blocker — type=_date_arg라 date 객체, fromisoformat 금지)."""

    def test_fixed_range_uses_date_objects(self):
        # args.start_date/end_date는 date (type=_date_arg) → 그대로 사용 (fromisoformat TypeError 회귀 차단)
        args = SimpleNamespace(start_date=date(2025, 6, 2), end_date=date(2026, 5, 20))
        ws, we = B._resolve_range_dry_run_window(args)
        self.assertEqual(ws, date(2025, 6, 2))
        self.assertEqual(we, date(2026, 5, 20))

    def test_one_sided_returns_none(self):
        args = SimpleNamespace(start_date=date(2025, 6, 2), end_date=None)
        self.assertIsNone(B._resolve_range_dry_run_window(args))
        args2 = SimpleNamespace(start_date=None, end_date=date(2026, 5, 20))
        self.assertIsNone(B._resolve_range_dry_run_window(args2))

    def test_no_dates_rolling_1year(self):
        args = SimpleNamespace(start_date=None, end_date=None)
        ws, we = B._resolve_range_dry_run_window(args)
        self.assertLess(ws, we)                          # today-1 rolling
        self.assertIn((we - ws).days, range(360, 368))   # calendar-year lookback ≈ 365~366


if __name__ == "__main__":
    unittest.main()
