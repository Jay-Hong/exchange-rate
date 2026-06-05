"""Investing observed_eod → source_daily_rates writer 테스트 (ADR-035 D1).

검증:
  A. process_date 분류 — write (관측 >=1) / skip (관측 0, no_observation)
  B. build_investing_eod_row rollup — close=last / high=max / low=min / point_count / metadata
  C. write_with_transaction — commit + idempotent + 혼합/unsupported asset 거부
  D. _run_write 종료 코드 — clean write / skip-only (atomicity abort 없음, Hana와 차이)
  E. 통화 파라미터화 (jpy/eur)
  F. production guard
"""
from __future__ import annotations

import argparse
import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# scripts/ 경로 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import models  # noqa: E402
from app.models import InvestingExchangeRate, SourceDailyRate  # noqa: E402
import backfill_investing_source_daily_rates as W  # noqa: E402


def _ts(y, mo, d, h, mi=0):
    """naive UTC datetime (investing_exchange_rates.timestamp 형식)."""
    return datetime(y, mo, d, h, mi)


# 공통 target date — 2026-06-04 (Thu). KST window = UTC [2026-06-03 15:00, 2026-06-04 15:00)
WEEKDAY = date(2026, 6, 4)


class _BaseDBTest(unittest.TestCase):
    """in-memory SQLite + write-path helper patch 공통 setup."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self._p_session = patch("app.database.SessionLocal", self.Session)
        self._p_engine = patch("app.database.engine", self.engine)
        self._p_session.start()
        self._p_engine.start()

    def tearDown(self):
        self._p_session.stop()
        self._p_engine.stop()
        self.engine.dispose()

    def _add_inv(self, ts, rate, currency="usd-krw"):
        with self.Session() as db:
            db.add(InvestingExchangeRate(currency=currency, rate=rate, timestamp=ts))
            db.commit()

    def _daily_rows(self):
        with self.Session() as db:
            return db.query(SourceDailyRate).filter(SourceDailyRate.source == "investing").all()


# ---------------------------------------------------------------------------
# Group A — process_date 분류
# ---------------------------------------------------------------------------

class TestProcessDateClassification(_BaseDBTest):

    def test_write_with_observations(self):
        """당일 관측 >=1 → write."""
        self._add_inv(_ts(2026, 6, 3, 20, 0), 1380.0)  # KST 6/4 05:00
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1385.0)   # KST 6/4 11:00
        self._add_inv(_ts(2026, 6, 4, 8, 0), 1382.0)   # KST 6/4 17:00 = close
        with self.Session() as db:
            action, row, code = W.process_date(db, WEEKDAY)
        self.assertEqual(action, "write")
        self.assertEqual(row["close"], Decimal("1382.0"))
        self.assertEqual(row["rate"], row["close"])           # invariant
        self.assertEqual(row["high"], Decimal("1385.0"))      # max
        self.assertEqual(row["low"], Decimal("1380.0"))       # min
        self.assertEqual(row["metadata_json"]["point_count"], 3)

    def test_skip_no_observation(self):
        """당일 관측 0 → skip (no_observation, Hana의 business_day_error 아님)."""
        # 다른 날짜 관측만 추가 (6/4 KST window 밖)
        self._add_inv(_ts(2026, 6, 5, 2, 0), 1390.0)   # KST 6/5
        with self.Session() as db:
            action, row, code = W.process_date(db, WEEKDAY)
        self.assertEqual(action, "skip")
        self.assertEqual(code, "no_observation")
        self.assertIsNone(row)

    def test_window_isolation(self):
        """KST day window 정확 — 인접일 관측이 섞이지 않음."""
        self._add_inv(_ts(2026, 6, 3, 14, 0), 9999.0)  # UTC 6/3 14:00 = KST 6/3 23:00 (6/4 window 밖)
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1385.0)   # KST 6/4 11:00 (window 안)
        self._add_inv(_ts(2026, 6, 4, 15, 0), 8888.0)  # UTC 6/4 15:00 = KST 6/5 00:00 (window 밖, exclusive)
        with self.Session() as db:
            action, row, _ = W.process_date(db, WEEKDAY)
        self.assertEqual(action, "write")
        self.assertEqual(row["metadata_json"]["point_count"], 1)  # 1385 하나만
        self.assertEqual(row["close"], Decimal("1385.0"))


# ---------------------------------------------------------------------------
# Group B — build rollup
# ---------------------------------------------------------------------------

class TestBuildRow(_BaseDBTest):

    def test_single_observation(self):
        """관측 1개 → high=low=close (당일 단일점)."""
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1384.5)
        with self.Session() as db:
            _, row, _ = W.process_date(db, WEEKDAY)
        self.assertEqual(row["high"], Decimal("1384.5"))
        self.assertEqual(row["low"], Decimal("1384.5"))
        self.assertEqual(row["close"], Decimal("1384.5"))
        self.assertEqual(row["metadata_json"]["point_count"], 1)

    def test_row_provenance(self):
        """provenance/nullable/source 필드 — investing observed 방향."""
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1385.0)
        with self.Session() as db:
            _, row, _ = W.process_date(db, WEEKDAY)
        self.assertEqual(row["source"], "investing")
        self.assertEqual(row["asset"], "usd-krw")
        self.assertEqual(row["close_basis"], "investing_observed_eod")
        self.assertEqual(row["source_method"], "observed_rollup")
        self.assertEqual(row["ohlc_quality"], "observed_rollup")
        self.assertIsNone(row["contract_code"])
        self.assertIsNone(row["basis_date"])
        self.assertIsNone(row["published_at"])
        self.assertEqual(row["metadata_json"]["source_table"], "investing_exchange_rates")
        self.assertIn("first_ts_kst", row["metadata_json"])
        self.assertIn("last_ts_kst", row["metadata_json"])

    def test_float_artifact_quantized_to_6_places(self):
        """Float 연산 artifact(13자리) → quantize 6 places (range-dry-run에서 발견된 실제 JPY 케이스)."""
        # 921.6100000000001 = JPY/KRW per-100 환산 류 binary float artifact (str()이 13자리 보존)
        self._add_inv(_ts(2026, 6, 4, 2, 0), 921.6100000000001, currency="jpy-krw")
        self._add_inv(_ts(2026, 6, 4, 8, 0), 932.5100000000001, currency="jpy-krw")
        with self.Session() as db:
            _, row, _ = W.process_date(db, WEEKDAY, currency="jpy-krw")
        # quantize → 소수부 <= 6, precision validation 0건 (quantize 없으면 13자리로 실패)
        self.assertEqual(W.validate_decimal_precision(row), [])
        for f in ("rate", "high", "low", "close"):
            self.assertGreaterEqual(row[f].as_tuple().exponent, -6, f"{f}={row[f]}")
        # 값 보존 (artifact만 제거)
        self.assertEqual(row["close"], Decimal("932.51"))  # last obs
        self.assertEqual(row["high"], Decimal("932.51"))   # max
        self.assertEqual(row["low"], Decimal("921.61"))    # min


# ---------------------------------------------------------------------------
# Group C — write transaction
# ---------------------------------------------------------------------------

class TestWriteTransaction(_BaseDBTest):

    def _build_row(self, target=WEEKDAY, currency="usd-krw"):
        with self.Session() as db:
            _, row, _ = W.process_date(db, target, currency=currency)
        return row

    def test_write_commits(self):
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1385.0)
        row = self._build_row()
        oc = W.write_with_transaction_investing([row])
        self.assertTrue(oc.success, oc.issues)
        self.assertEqual(oc.inserted_dates, [WEEKDAY])  # 신규 insert
        self.assertEqual(oc.updated_dates, [])
        rows = self._daily_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source, "investing")
        self.assertEqual(rows[0].asset, "usd-krw")
        self.assertEqual(rows[0].close, Decimal("1385.0"))

    def test_idempotent_rerun_is_update_not_insert(self):
        """재실행 → updated (inserted 아님) → rollback anchor가 기존 row 안 지움 (Codex Finding 1)."""
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1385.0)
        row = self._build_row()
        oc1 = W.write_with_transaction_investing([row])
        self.assertEqual(oc1.inserted_dates, [WEEKDAY])
        oc2 = W.write_with_transaction_investing([row])  # 재실행
        self.assertTrue(oc2.success, oc2.issues)
        self.assertEqual(oc2.inserted_dates, [])          # 재실행은 inserted 0
        self.assertEqual(oc2.updated_dates, [WEEKDAY])    # updated (전체 삭제 anchor 금지 대상)
        self.assertEqual(len(self._daily_rows()), 1)      # 중복 없음

    def test_require_empty_target_rejects_existing(self):
        """--require-empty-target — 기존 row 있으면 reject (gap-only 강제, rollback 안전)."""
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1385.0)
        row = self._build_row()
        W.write_with_transaction_investing([row])  # 첫 write (existing 생성)
        oc = W.write_with_transaction_investing([row], require_empty_target=True)
        self.assertFalse(oc.success)
        self.assertTrue(any("require-empty-target" in i for i in oc.issues), oc.issues)
        self.assertEqual(oc.inserted_dates, [])

    def test_mixed_asset_rejected(self):
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1385.0, currency="usd-krw")
        self._add_inv(_ts(2026, 6, 4, 2, 0), 9.05, currency="jpy-krw")
        with self.Session() as db:
            _, usd_row, _ = W.process_date(db, WEEKDAY, currency="usd-krw")
            _, jpy_row, _ = W.process_date(db, WEEKDAY, currency="jpy-krw")
        oc = W.write_with_transaction_investing([usd_row, jpy_row])
        self.assertFalse(oc.success)
        self.assertTrue(any("혼합 asset" in i for i in oc.issues), oc.issues)
        self.assertEqual(len(self._daily_rows()), 0)

    def test_unsupported_asset_rejected(self):
        self._add_inv(_ts(2026, 6, 4, 2, 0), 100.0, currency="xxx-krw")
        with self.Session() as db:
            _, row, _ = W.process_date(db, WEEKDAY, currency="xxx-krw")
        self.assertEqual(row["asset"], "xxx-krw")  # build는 막지 않음
        oc = W.write_with_transaction_investing([row])
        self.assertFalse(oc.success)
        self.assertTrue(any("unsupported asset" in i for i in oc.issues), oc.issues)
        self.assertEqual(len(self._daily_rows()), 0)


# ---------------------------------------------------------------------------
# Group D — _run_write 종료 코드
# ---------------------------------------------------------------------------

class TestRunWriteExitCodes(_BaseDBTest):

    def _args(self, start, end, include_today=False, allow_production=False, currency="usd-krw",
              require_empty_target=False, expected_min_rows=None):
        return argparse.Namespace(
            start_date=start, end_date=end,
            include_today=include_today, allow_production_write=allow_production,
            currency=currency, require_empty_target=require_empty_target,
            expected_min_rows=expected_min_rows,
        )

    def _run(self, args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = W._run_write(args)
        return rc, buf.getvalue()

    def test_clean_write_exit_0(self):
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1385.0)
        rc, out = self._run(self._args(WEEKDAY, WEEKDAY))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._daily_rows()), 1)

    def test_skip_only_exit_0(self):
        """관측 0인 날만 있으면 write 0 rows, exit 0 (Investing skip은 정상 — atomicity abort 없음)."""
        # 관측 없음 (다른 날만)
        self._add_inv(_ts(2026, 6, 6, 2, 0), 1390.0)
        rc, out = self._run(self._args(WEEKDAY, WEEKDAY))
        self.assertEqual(rc, 0)  # Hana면 business_day_no_changes로 exit 1이지만, Investing은 0
        self.assertEqual(len(self._daily_rows()), 0)

    def test_mixed_range_skip_and_write(self):
        """range에 write일 + skip일 혼재 → write일만 적재, skip은 정상 (abort 없음).

        range 6/3~6/4 (둘 다 today 전 — validate_write_range "end < today" 가드 만족).
        """
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1385.0)  # 6/4 write (KST 6/4 11:00)
        # 6/3은 관측 없음 → skip (no_observation)
        rc, out = self._run(self._args(date(2026, 6, 3), date(2026, 6, 4)))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._daily_rows()), 1)  # 6/4만 (6/3 skip)

    def test_multi_day_zero_rows_fail(self):
        """multi-day range인데 관측 0 → fail-close (Codex Finding 3 — silent PASS 차단)."""
        self._add_inv(_ts(2026, 6, 10, 2, 0), 1390.0)  # range 밖 관측만
        rc, out = self._run(self._args(date(2026, 6, 2), date(2026, 6, 4)))
        self.assertEqual(rc, 1)
        self.assertIn("multi-day range", out)
        self.assertEqual(len(self._daily_rows()), 0)

    def test_expected_min_rows_fail(self):
        """--expected-min-rows 미달 → fail-close (Codex Finding 3 — 부족 mask 차단)."""
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1385.0)  # 1 row
        rc, out = self._run(self._args(WEEKDAY, WEEKDAY, expected_min_rows=5))
        self.assertEqual(rc, 1)
        self.assertIn("expected-min-rows", out)
        self.assertEqual(len(self._daily_rows()), 0)  # write 안 됨


# ---------------------------------------------------------------------------
# Group E — 통화 파라미터화
# ---------------------------------------------------------------------------

class TestCurrencyParameterization(_BaseDBTest):

    def test_jpy(self):
        self._add_inv(_ts(2026, 6, 4, 2, 0), 9.00, currency="jpy-krw")
        self._add_inv(_ts(2026, 6, 4, 8, 0), 9.05, currency="jpy-krw")
        with self.Session() as db:
            action, row, _ = W.process_date(db, WEEKDAY, currency="jpy-krw")
        self.assertEqual(action, "write")
        self.assertEqual(row["asset"], "jpy-krw")
        self.assertEqual(row["close"], Decimal("9.05"))
        oc = W.write_with_transaction_investing([row])
        self.assertTrue(oc.success, oc.issues)
        self.assertEqual(self._daily_rows()[0].asset, "jpy-krw")

    def test_eur(self):
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1600.0, currency="eur-krw")
        with self.Session() as db:
            action, row, _ = W.process_date(db, WEEKDAY, currency="eur-krw")
        self.assertEqual(action, "write")
        self.assertEqual(row["asset"], "eur-krw")

    def test_currency_isolation(self):
        """usd + jpy 공존 → currency별 조회 독립."""
        self._add_inv(_ts(2026, 6, 4, 2, 0), 1385.0, currency="usd-krw")
        self._add_inv(_ts(2026, 6, 4, 6, 0), 9.05, currency="jpy-krw")
        with self.Session() as db:
            _, usd_row, _ = W.process_date(db, WEEKDAY, currency="usd-krw")
            _, jpy_row, _ = W.process_date(db, WEEKDAY, currency="jpy-krw")
        self.assertEqual(usd_row["close"], Decimal("1385.0"))
        self.assertEqual(jpy_row["close"], Decimal("9.05"))


# ---------------------------------------------------------------------------
# Group F — production guard
# ---------------------------------------------------------------------------

class TestProductionGuard(unittest.TestCase):

    def _fake_engine(self, dialect_name):
        eng = MagicMock()
        eng.url.get_dialect.return_value.name = dialect_name
        eng.url.host = "fake-host"
        return eng

    def test_rejects_non_sqlite_without_allow(self):
        with patch("app.database.engine", self._fake_engine("postgresql")):
            err = W.check_production_write_guard(allow_production=False)
        self.assertIsNotNone(err)
        self.assertIn("production write 차단", err)
        self.assertIn("***", err)  # host redacted

    def test_allows_non_sqlite_with_flag(self):
        with patch("app.database.engine", self._fake_engine("postgresql")):
            err = W.check_production_write_guard(allow_production=True)
        self.assertIsNone(err)

    def test_sqlite_always_passes(self):
        with patch("app.database.engine", self._fake_engine("sqlite")):
            err = W.check_production_write_guard(allow_production=False)
        self.assertIsNone(err)


# ---------------------------------------------------------------------------
# Group G — CLI combo fail-close (Codex Finding 4)
# ---------------------------------------------------------------------------

class TestCliComboGuards(unittest.TestCase):
    """--require-empty-target / --expected-min-rows는 --write 전용 + 음수 reject.

    combo 체크는 DB/app import 전 early fail → subprocess로 빠르게 검증 (Hana CliEmitGuards 패턴).
    """
    REPO = Path(__file__).resolve().parent.parent
    SCRIPT = str(REPO / "scripts" / "backfill_investing_source_daily_rates.py")

    def _run(self, args):
        import subprocess
        return subprocess.run(
            [sys.executable, self.SCRIPT, *args],
            capture_output=True, text=True, cwd=str(self.REPO),
        )

    def test_require_empty_without_write_rejected(self):
        r = self._run(["--require-empty-target"])
        self.assertEqual(r.returncode, 1, msg=r.stdout[-300:])
        self.assertIn("CONFIG 실패", r.stdout)
        self.assertIn("--write 전용", r.stdout)

    def test_expected_min_without_write_rejected(self):
        r = self._run(["--expected-min-rows", "5"])
        self.assertEqual(r.returncode, 1, msg=r.stdout[-300:])
        self.assertIn("CONFIG 실패", r.stdout)
        self.assertIn("--write 전용", r.stdout)

    def test_negative_expected_min_rejected(self):
        r = self._run(["--expected-min-rows=-1"])
        self.assertEqual(r.returncode, 1, msg=r.stdout[-300:])
        self.assertIn(">= 1", r.stdout)

    def test_range_args_without_mode_rejected(self):
        """--start-date/--end-date를 mode 없이 → reject (조용히 today 단일 dry-run 빠지는 것 차단, Codex F6)."""
        r = self._run(["--start-date", "2026-06-01", "--end-date", "2026-06-02"])
        self.assertEqual(r.returncode, 1, msg=r.stdout[-300:])
        self.assertIn("--write 또는 --range-dry-run 전용", r.stdout)

    def test_include_today_without_mode_rejected(self):
        r = self._run(["--include-today"])
        self.assertEqual(r.returncode, 1, msg=r.stdout[-300:])
        self.assertIn("--include-today", r.stdout)

    def test_allow_production_without_write_rejected(self):
        r = self._run(["--range-dry-run", "--start-date", "2026-06-01", "--end-date", "2026-06-02",
                       "--allow-production-write"])
        self.assertEqual(r.returncode, 1, msg=r.stdout[-300:])
        self.assertIn("--allow-production-write는 --write 전용", r.stdout)

    def test_date_with_range_mode_rejected(self):
        """대칭 케이스 — --date(단일 전용)를 range mode와 같이 → reject (역방향 mode 혼동)."""
        r = self._run(["--range-dry-run", "--date", "2026-06-01",
                       "--start-date", "2026-06-01", "--end-date", "2026-06-02"])
        self.assertEqual(r.returncode, 1, msg=r.stdout[-300:])
        self.assertIn("--date는 단일일 dry-run 전용", r.stdout)


if __name__ == "__main__":
    unittest.main()
