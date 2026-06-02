"""Hana official writer overlap guard + conditional upsert 단위 테스트 (ADR-034 Phase 2d Step 4A Stage 1).

`scripts/backfill_hana_source_daily_rates.py`의 `write_with_transaction_hana()` overlap guard +
`app/source_daily_rates.py` upsert의 conditional conflict update 영구 회귀 검증.

corruption 차단 2중:
  1. pre-write overlap guard — expected_dates에 non-official row가 있으면 reject (빠른 차단)
  2. conditional conflict update — absent key에 concurrent observed insert가 들어와도
     `update_only_if_existing_close_basis`로 update skip → 기존 observed 보존 (race-safe)

rollback 안전: HanaWriteOutcome.inserted_dates / updated_dates 구분 → 기존 official 미삭제.

In-memory SQLite — 외부 DB 의존성 0 (test_hana_observed_eod_writer.py 패턴).
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import models  # noqa: E402
from app.models import SourceDailyRate  # noqa: E402
from app.source_daily_rates import upsert as upsert_fn  # noqa: E402
import backfill_hana_source_daily_rates as W  # noqa: E402


def _official_row(d: date, close: str = "1500.0") -> dict:
    """write_with_transaction_hana가 기대하는 official-shape row dict (post-write 검증 통과)."""
    c = Decimal(close)
    return {
        "source": "hana", "asset": "usd-krw", "date_kst": d,
        "rate": c, "close": c, "high": c, "low": c,  # close_only: rate=close, high=low=close
        "ohlc_quality": "close_only",
        "close_basis": "hana_official_historical_backfill",
        "source_method": "external_backfill",
        "contract_code": None, "basis_date": d,
        "published_at": datetime(2026, 6, 1, 7, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        "metadata_json": {"pbldSqn": "1081"},
    }


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

    def _seed_observed(self, d: date, close: str = "1500.0"):
        c = Decimal(close)
        with self.Session() as db:
            db.add(SourceDailyRate(
                source="hana", asset="usd-krw", date_kst=d,
                rate=c, high=c, low=c, close=c,
                ohlc_quality="observed_rollup", close_basis="hana_observed_eod",
                source_method="observed_rollup",
                contract_code=None, basis_date=None, published_at=None,
                metadata_json={"rollup_mode": "observed"},
            ))
            db.commit()

    def _seed_official(self, d: date, close: str = "1500.0"):
        c = Decimal(close)
        with self.Session() as db:
            db.add(SourceDailyRate(
                source="hana", asset="usd-krw", date_kst=d,
                rate=c, high=c, low=c, close=c,
                ohlc_quality="close_only", close_basis="hana_official_historical_backfill",
                source_method="external_backfill",
                contract_code=None, basis_date=d, published_at=datetime(2026, 5, 28, 7, 0),
                metadata_json={"pbldSqn": "1080"},
            ))
            db.commit()

    def _get(self, d: date):
        with self.Session() as db:
            return db.query(SourceDailyRate).filter_by(
                source="hana", asset="usd-krw", date_kst=d).one()


class TestOverlapGuard(_BaseDBTest):

    def test_observed_row_in_range_rejected_and_preserved(self):
        """[1] expected_dates에 observed row → reject + observed canonical 보존."""
        d = date(2026, 5, 28)
        self._seed_observed(d, "1496.1")
        out = W.write_with_transaction_hana([_official_row(d, "1502.0")], "USD", d, d)
        self.assertFalse(out.success)
        self.assertTrue(any("overlap guard" in i for i in out.issues), out.issues)
        r = self._get(d)
        self.assertEqual(r.close_basis, "hana_observed_eod")
        self.assertEqual(Decimal(str(r.close)), Decimal("1496.1"))

    def test_guard_detects_overlap_on_fallback_date_not_request_range(self):
        """[2] 휴일 fallback → rows_to_write date_kst=F. request range가 H여도 expected_dates(F) 기반 탐지."""
        fallback = date(2026, 5, 22)
        self._seed_observed(fallback, "1500.0")
        out = W.write_with_transaction_hana(
            [_official_row(fallback, "1512.0")], "USD", date(2026, 5, 24), date(2026, 5, 24))
        self.assertFalse(out.success)
        self.assertTrue(any("overlap guard" in i for i in out.issues), out.issues)
        self.assertEqual(self._get(fallback).close_basis, "hana_observed_eod")

    def test_official_official_overlap_idempotent_rerun_allowed(self):
        """[3] 기존 official row와 overlap → idempotent rerun 허용 + outcome.updated_dates에 기록."""
        d = date(2026, 5, 27)
        self._seed_official(d, "1502.0")
        out = W.write_with_transaction_hana([_official_row(d, "1502.0")], "USD", d, d)
        self.assertTrue(out.success, out.issues)
        self.assertEqual(out.updated_dates, [d])      # 기존 official re-upsert
        self.assertEqual(out.inserted_dates, [])      # 신규 insert 없음
        self.assertEqual(self._get(d).close_basis, "hana_official_historical_backfill")

    def test_gap_only_all_inserted(self):
        """[sanity] 기존 row 없음(gap-only) → 정상 write + inserted_dates에 전부."""
        d = date(2026, 5, 21)
        out = W.write_with_transaction_hana([_official_row(d, "1505.5")], "USD", d, d)
        self.assertTrue(out.success, out.issues)
        self.assertEqual(out.inserted_dates, [d])
        self.assertEqual(out.updated_dates, [])

    def test_inserted_updated_split(self):
        """[rollback] 기존 official 1 + 신규 1 mixed → inserted=[new], updated=[existing]."""
        existing = date(2026, 5, 26)
        new = date(2026, 5, 27)
        self._seed_official(existing, "1507.5")
        out = W.write_with_transaction_hana(
            [_official_row(existing, "1507.5"), _official_row(new, "1502.0")],
            "USD", existing, new)
        self.assertTrue(out.success, out.issues)
        self.assertEqual(out.inserted_dates, [new])
        self.assertEqual(out.updated_dates, [existing])

    def test_require_empty_target_rejects_existing(self):
        """[Codex #5] require_empty_target=True + 기존 official row → reject (gap-only 강제)."""
        d = date(2026, 5, 27)
        self._seed_official(d, "1502.0")
        out = W.write_with_transaction_hana(
            [_official_row(d, "1502.0")], "USD", d, d, require_empty_target=True)
        self.assertFalse(out.success)
        self.assertTrue(any("require-empty-target" in i for i in out.issues), out.issues)
        # 기존 official 보존 (rollback)
        self.assertEqual(self._get(d).close_basis, "hana_official_historical_backfill")

    def test_require_empty_target_allows_gap_only(self):
        """[Codex #5] require_empty_target=True + 기존 row 없음(gap-only) → 정상 write."""
        d = date(2026, 5, 21)
        out = W.write_with_transaction_hana(
            [_official_row(d, "1505.5")], "USD", d, d, require_empty_target=True)
        self.assertTrue(out.success, out.issues)
        self.assertEqual(out.inserted_dates, [d])


class TestWritePathValidation(_BaseDBTest):
    """[Codex High] --write 경로도 self fail-close — pre-write _ROW_DRY_RUN_CHECKS로 비정상 row 차단.

    range dry-run을 생략한 직접 write여도 endpoint 이상값/parser 회귀가 commit되지 않아야 함.
    """

    def _count(self):
        with self.Session() as db:
            return db.query(SourceDailyRate).count()

    def test_negative_rate_rejected_zero_committed(self):
        d = date(2026, 5, 20)
        out = W.write_with_transaction_hana([_official_row(d, "-1.0")], "USD", d, d)
        self.assertFalse(out.success)
        self.assertTrue(any("pre-write validation" in i for i in out.issues), out.issues)
        self.assertEqual(self._count(), 0)   # atomic — 0 committed

    def test_precision_overflow_rejected_zero_committed(self):
        d = date(2026, 5, 20)
        # 소수 7자리 (Decimal(14,6) 초과)
        out = W.write_with_transaction_hana([_official_row(d, "1500.1234567")], "USD", d, d)
        self.assertFalse(out.success)
        self.assertEqual(self._count(), 0)

    def test_mixed_invalid_atomic_abort_zero_committed(self):
        """range 중 하나만 invalid여도 전체 atomic abort (valid row도 미적재)."""
        good = date(2026, 5, 20)
        bad = date(2026, 5, 21)
        out = W.write_with_transaction_hana(
            [_official_row(good, "1500.0"), _official_row(bad, "-2.0")], "USD", good, bad)
        self.assertFalse(out.success)
        self.assertEqual(self._count(), 0)   # valid row도 commit 안 됨 (atomicity)


class TestSharedUpsertConditional(_BaseDBTest):
    """공용 upsert update_only_if_existing_close_basis — absent-key concurrent race 차단 (High 1)."""

    def test_conditional_skip_preserves_existing_observed(self):
        """기존 observed row에 conditional official upsert → skip (close_basis 불일치) → observed 보존."""
        d = date(2026, 5, 28)
        self._seed_observed(d, "1496.1")
        with self.Session() as db:
            upsert_fn(
                db, source="hana", asset="usd-krw", date_kst=d,
                close=Decimal("1502.0"), ohlc_quality="close_only",
                close_basis="hana_official_historical_backfill",
                source_method="external_backfill", high=Decimal("1502.0"), low=Decimal("1502.0"),
                basis_date=d, published_at=datetime(2026, 6, 1, 7, 0),
                metadata_json={"pbldSqn": "1081"},
                update_only_if_existing_close_basis="hana_official_historical_backfill",
            )
        r = self._get(d)
        # observed 보존 (conditional skip)
        self.assertEqual(r.close_basis, "hana_observed_eod")
        self.assertEqual(Decimal(str(r.close)), Decimal("1496.1"))

    def test_default_unconditional_overwrites(self):
        """[sanity] param 미지정(default None) → 기존 동작 유지 (무조건 overwrite)."""
        d = date(2026, 5, 28)
        self._seed_observed(d, "1496.1")
        with self.Session() as db:
            upsert_fn(
                db, source="hana", asset="usd-krw", date_kst=d,
                close=Decimal("1502.0"), ohlc_quality="close_only",
                close_basis="hana_official_historical_backfill",
                source_method="external_backfill", high=Decimal("1502.0"), low=Decimal("1502.0"),
                basis_date=d, published_at=datetime(2026, 6, 1, 7, 0),
                metadata_json={"pbldSqn": "1081"},
            )
        self.assertEqual(self._get(d).close_basis, "hana_official_historical_backfill")

    def test_conditional_updates_existing_official(self):
        """기존 official row에 conditional official upsert → update 진행 (close_basis 일치)."""
        d = date(2026, 5, 27)
        self._seed_official(d, "1502.0")
        with self.Session() as db:
            upsert_fn(
                db, source="hana", asset="usd-krw", date_kst=d,
                close=Decimal("1503.0"), ohlc_quality="close_only",
                close_basis="hana_official_historical_backfill",
                source_method="external_backfill", high=Decimal("1503.0"), low=Decimal("1503.0"),
                basis_date=d, published_at=datetime(2026, 6, 1, 7, 0),
                metadata_json={"pbldSqn": "1082"},
                update_only_if_existing_close_basis="hana_official_historical_backfill",
            )
        self.assertEqual(Decimal(str(self._get(d).close)), Decimal("1503.0"))


class TestConditionalUpsertCompile(unittest.TestCase):
    """conditional ON CONFLICT DO UPDATE WHERE가 PG·SQLite 양쪽 compile (dialect 회귀)."""

    def test_pg_and_sqlite_compile_with_where(self):
        from sqlalchemy.dialects.postgresql import insert as pg_insert, dialect as pg_dialect
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert, dialect as sqlite_dialect
        for ins, dia in [(pg_insert, pg_dialect()), (sqlite_insert, sqlite_dialect())]:
            stmt = ins(SourceDailyRate).values(source="hana", asset="usd-krw")
            stmt = stmt.on_conflict_do_update(
                index_elements=["source", "asset", "date_kst"],
                set_={"close": stmt.excluded.close},
                where=(SourceDailyRate.close_basis == "hana_official_historical_backfill"),
            )
            sql = str(stmt.compile(dialect=dia)).upper()
            self.assertIn("WHERE", sql)


def _run_main(argv) -> tuple[int, str]:
    """main()을 argv로 실행 → (exit_code, stdout). exit_code=0 = 정상 return."""
    out = io.StringIO()
    code = 0
    with patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
        try:
            W.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue()


def _parsed(basis: date, rate: str = "1512.0", sqn: int = 1081) -> dict:
    """parse_response 반환 형태 mock (build_row 입력)."""
    return {
        "basis_date": basis,
        "published_at": datetime(2026, 5, 23, 7, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        "pbld_sqn": sqn,
        "rate_dec": Decimal(rate),
        "txt_ar_count": 10,
    }


class TestRangeDryRun(unittest.TestCase):
    """--range-dry-run: DB-free pre-batch validation gate."""

    ARGV = ["prog", "--range-dry-run", "--start-date", "2026-05-21", "--end-date", "2026-05-27"]

    def test_fetch_error_exit_1(self):
        """fetch error 1건 → exit 1 (write 진입 차단 등가)."""
        with patch.object(W, "fetch_and_dedup_calendar_range",
                          return_value=([], ["fetch error: timeout 2026-05-22"], [])):
            code, out = _run_main(self.ARGV)
        self.assertEqual(code, 1)
        self.assertIn("RANGE DRY-RUN 실패", out)
        self.assertIn("fetch error", out)

    def test_real_fallback_dedup_path(self):
        """[Codex #2] 실제 fetch_and_dedup_calendar_range 경로 (fetch_html/parse만 mock) —
        휴일 fallback로 3 calendar day가 basis_date 5/22 1개로 dedup + fallback 2건 surface."""
        # 5/22(Fri)·5/23(Sat)·5/24(Sun) — 23/24는 휴일이라 basis_date=5/22로 fallback
        argv = ["prog", "--range-dry-run", "--start-date", "2026-05-22", "--end-date", "2026-05-24"]
        with patch.object(W, "fetch_html", return_value="<html/>"), \
             patch.object(W, "parse_response", return_value=_parsed(date(2026, 5, 22))):
            code, out = _run_main(argv)
        self.assertEqual(code, 0, out)
        self.assertIn("RANGE DRY-RUN 완료", out)
        self.assertIn("rows (basis_date dedup): 1", out)          # 실제 dedup
        self.assertIn("basis_date=2026-05-22", out)               # 실제 fallback event surface

    def test_reverse_range_rejected(self):
        """[Codex #1] 역순 range → validate_write_range로 fail-close exit 1 (silent pass 차단)."""
        code, out = _run_main(
            ["prog", "--range-dry-run", "--start-date", "2026-05-27", "--end-date", "2026-05-21"])
        self.assertEqual(code, 1)
        self.assertIn("start_date=2026-05-27 > end_date=2026-05-21", out)

    def test_empty_range_fail_close(self):
        """[Codex #1] rows 0 + fetch error 0 → fail-close exit 1 (gate)."""
        with patch.object(W, "fetch_and_dedup_calendar_range", return_value=([], [], [])):
            code, out = _run_main(self.ARGV)
        self.assertEqual(code, 1)
        self.assertIn("rows 0 + fetch error 0", out)

    def test_rejects_with_write(self):
        code, out = _run_main(self.ARGV + ["--write"])
        self.assertEqual(code, 1)
        self.assertIn("동시 사용 금지", out)

    def test_rejects_with_date(self):
        """[Codex #3] --range-dry-run + --date → 모호 조합 fail-close."""
        code, out = _run_main(self.ARGV + ["--date", "2026-05-21"])
        self.assertEqual(code, 1)
        self.assertIn("--date(단일일 dry-run)는", out)

    def test_requires_start_end(self):
        code, out = _run_main(["prog", "--range-dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("--start-date / --end-date 필수", out)

    def test_start_end_without_mode_rejected(self):
        """[Codex #3] mode 없이 start/end만 → 조용히 무시 대신 fail-close."""
        code, out = _run_main(["prog", "--start-date", "2026-05-21", "--end-date", "2026-05-27"])
        self.assertEqual(code, 1)
        self.assertIn("--write 또는 --range-dry-run과 함께만 유효", out)


class TestCliCombo(unittest.TestCase):
    """[Codex 필수1] mode/flag 조합 fail-close (조용히 무시되는 조합 차단, _validate_cli_combo 한 곳)."""

    def test_write_plus_date_rejected(self):
        code, out = _run_main(
            ["prog", "--write", "--date", "2026-05-21", "--start-date", "2026-05-21",
             "--end-date", "2026-05-21", "--allow-production-write"])
        self.assertEqual(code, 1)
        self.assertIn("--date(단일일 dry-run)는", out)

    def test_range_dry_run_plus_require_empty_rejected(self):
        code, out = _run_main(
            ["prog", "--range-dry-run", "--start-date", "2026-05-21", "--end-date", "2026-05-27",
             "--require-empty-target"])
        self.assertEqual(code, 1)
        self.assertIn("--require-empty-target는 --write 전용", out)

    def test_require_empty_target_alone_rejected(self):
        code, out = _run_main(["prog", "--require-empty-target"])
        self.assertEqual(code, 1)
        self.assertIn("--require-empty-target는 --write 전용", out)

    def test_allow_production_without_write_rejected(self):
        code, out = _run_main(["prog", "--allow-production-write"])
        self.assertEqual(code, 1)
        self.assertIn("--allow-production-write는 --write 전용", out)

    def test_include_today_without_mode_rejected(self):
        code, out = _run_main(["prog", "--include-today"])
        self.assertEqual(code, 1)
        self.assertIn("--include-today는", out)


class TestValidateWriteRange(unittest.TestCase):
    """[Codex 권장] range gate의 intraday/include-today 결정론적 검증 (validate_write_range 직접)."""

    def test_reverse_rejected(self):
        err = W.validate_write_range(date(2026, 5, 27), date(2026, 5, 21), date(2026, 6, 1), False)
        self.assertIsNotNone(err)
        self.assertIn(">", err)

    def test_intraday_rejected_without_include_today(self):
        d = date(2026, 6, 1)
        err = W.validate_write_range(d, d, today=d, include_today=False)
        self.assertIsNotNone(err)
        self.assertIn("intraday", err)

    def test_include_today_allows_today(self):
        d = date(2026, 6, 1)
        err = W.validate_write_range(d, d, today=d, include_today=True)
        self.assertIsNone(err)

    def test_future_rejected_even_with_include_today(self):
        """[Codex High] 미래 date는 --include-today로도 reject (backfill은 과거만)."""
        err = W.validate_write_range(date(2026, 6, 2), date(2026, 6, 10), date(2026, 6, 2), True)
        self.assertIsNotNone(err)
        self.assertIn("미래", err)

    def test_past_range_ok(self):
        err = W.validate_write_range(date(2026, 5, 21), date(2026, 5, 27), date(2026, 6, 1), False)
        self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main()
