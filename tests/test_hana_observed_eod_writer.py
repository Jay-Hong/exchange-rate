"""Hana observed_eod writer 단위 테스트 (ADR-034 Phase 2d Step 3 Hana observed_eod 별 PR).

`scripts/backfill_hana_observed_eod_source_daily_rates.py`의 핵심 로직 영구 회귀 검증.
설계 검토 6 round (Codex 협업) + Blocker fix 2건 (range atomicity / write-path validation)
을 /tmp ephemeral smoke에서 영구 test로 승격.

In-memory SQLite — 외부 DB 의존성 0. write-path helper는 app.database.SessionLocal /
engine을 lazy import하므로 patch로 in-memory engine 주입 (DATABASE_URL override 미사용 —
shared-process singleton engine 오염 회피).

검증 그룹:
  A. process_date 분류/rollup — fresh / stale / missing baseline + weekday/weekend no-change
  B. write_with_transaction — commit + idempotent upsert + official overlap 안전망 (rollback)
  C. _run_write 종료 코드 — mixed range atomicity (B1) + negative raw reject (B2)
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

# scripts/ 경로 추가 (test_krx_baseline_extract.py 패턴)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import models  # noqa: E402
from app.models import BankExchangeRate, SourceDailyRate  # noqa: E402
import backfill_hana_observed_eod_source_daily_rates as W  # noqa: E402


def _ts(y, mo, d, h, mi=0):
    """naive UTC datetime (bank_exchange_rates.timestamp 형식)."""
    return datetime(y, mo, d, h, mi)


# 공통 target dates (역사 고정 — 시간 독립)
WEEKDAY = date(2026, 5, 28)    # Thursday (business_day). window [2026-05-27 15:00, 2026-05-28 15:00) UTC
SATURDAY = date(2026, 5, 30)   # Saturday
SUNDAY = date(2026, 5, 31)     # Sunday
HOLIDAY = date(2026, 5, 25)    # Monday 부처님오신날 대체공휴일 (KRX 사고 날짜). window [2026-05-24 15:00, 2026-05-25 15:00) UTC


class _BaseDBTest(unittest.TestCase):
    """in-memory SQLite + write-path helper patch 공통 setup."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        # write-path helper의 lazy `from app.database import SessionLocal/engine` 주입
        self._p_session = patch("app.database.SessionLocal", self.Session)
        self._p_engine = patch("app.database.engine", self.engine)
        self._p_session.start()
        self._p_engine.start()

    def tearDown(self):
        self._p_session.stop()
        self._p_engine.stop()
        self.engine.dispose()

    def _add_bank(self, ts, rate):
        with self.Session() as db:
            db.add(BankExchangeRate(bank="hana", currency="usd-krw", rate=rate, timestamp=ts))
            db.commit()

    def _daily_rows(self):
        with self.Session() as db:
            return db.query(SourceDailyRate).filter(SourceDailyRate.source == "hana").all()


# ---------------------------------------------------------------------------
# Group A — process_date 분류 / rollup
# ---------------------------------------------------------------------------

class TestProcessDateClassification(_BaseDBTest):

    def test_fresh_baseline_write(self):
        """평일 + changes + fresh prev → write, baseline 포함 rollup."""
        self._add_bank(_ts(2026, 5, 27, 10, 0), 1400.0)  # prev fresh (age 5h)
        self._add_bank(_ts(2026, 5, 28, 1, 0), 1402.0)   # change
        self._add_bank(_ts(2026, 5, 28, 5, 0), 1405.0)   # change = close
        with self.Session() as db:
            action, row, code = W.process_date(db, WEEKDAY)
        self.assertEqual(action, "write")
        self.assertEqual(row["close"], Decimal("1405.0"))
        self.assertEqual(row["rate"], row["close"])          # invariant
        self.assertEqual(row["high"], Decimal("1405.0"))
        self.assertEqual(row["low"], Decimal("1400.0"))      # prev baseline 포함
        md = row["metadata_json"]
        self.assertTrue(md["baseline_included"])
        self.assertIsNone(md["baseline_exclusion_reason"])
        self.assertEqual(md["calendar_class"], "business_day")
        self.assertEqual(md["day_change_count"], 2)
        self.assertEqual(md["rollup_point_count"], 3)
        # 항등식: rollup_point_count == day_change_count + int(baseline_included)
        self.assertEqual(md["rollup_point_count"], md["day_change_count"] + int(md["baseline_included"]))
        # nullable / enum
        self.assertIsNone(row["basis_date"])
        self.assertIsNone(row["published_at"])
        self.assertIsNone(row["contract_code"])
        self.assertEqual(row["close_basis"], "hana_observed_eod")
        self.assertEqual(row["source_method"], "observed_rollup")
        self.assertEqual(row["ohlc_quality"], "observed_rollup")

    def test_stale_baseline_excluded_but_write(self):
        """changes + stale prev (age>7d) → write, baseline 제외 but valid close 보존."""
        self._add_bank(_ts(2026, 5, 10, 10, 0), 1300.0)  # prev STALE (~17d)
        self._add_bank(_ts(2026, 5, 28, 5, 0), 1405.0)   # change = close
        with self.Session() as db:
            action, row, code = W.process_date(db, WEEKDAY)
        self.assertEqual(action, "write")
        md = row["metadata_json"]
        self.assertFalse(md["baseline_included"])
        self.assertEqual(md["baseline_exclusion_reason"], "older_than_7d")
        self.assertEqual(row["close"], Decimal("1405.0"))    # valid close 보존
        self.assertEqual(row["high"], Decimal("1405.0"))     # stale 1300 미포함
        self.assertEqual(row["low"], Decimal("1405.0"))
        self.assertEqual(md["rollup_point_count"], 1)        # baseline 제외
        self.assertGreater(md["carry_in_age_at_start_seconds"], 7 * 86400)
        self.assertIsNotNone(md["carry_in_raw_row_id"])      # 제외돼도 진단 기록

    def test_missing_baseline_write(self):
        """changes + prev None → write, baseline missing."""
        self._add_bank(_ts(2026, 5, 28, 5, 0), 1405.0)   # change only
        with self.Session() as db:
            action, row, code = W.process_date(db, WEEKDAY)
        self.assertEqual(action, "write")
        md = row["metadata_json"]
        self.assertFalse(md["baseline_included"])
        self.assertEqual(md["baseline_exclusion_reason"], "missing")
        self.assertIsNone(md["carry_in_age_at_start_seconds"])
        self.assertIsNone(md["carry_in_raw_row_id"])
        self.assertEqual(md["rollup_point_count"], 1)

    def test_business_day_no_changes_skip_error(self):
        """영업일(평일+비공휴일) + changes 0 → skip_error (business_day_no_changes)."""
        self._add_bank(_ts(2026, 5, 27, 10, 0), 1400.0)  # prev only
        with self.Session() as db:
            action, row, code = W.process_date(db, WEEKDAY)
        self.assertEqual(action, "skip_error")
        self.assertEqual(code, "business_day_no_changes")
        self.assertIsNone(row)

    def test_holiday_no_changes_skip_ok(self):
        """공휴일(평일) + changes 0 → skip_ok (holiday_no_changes). 2026-05-25 대체공휴일."""
        self._add_bank(_ts(2026, 5, 22, 10, 0), 1400.0)  # prev (금 5/22)
        with self.Session() as db:
            action, row, code = W.process_date(db, HOLIDAY)
        self.assertEqual(action, "skip_ok")
        self.assertEqual(code, "holiday_no_changes")
        self.assertIsNone(row)

    def test_holiday_changes_present_write(self):
        """공휴일 + changes 존재 → write + calendar_class="holiday" (드물지만 가능)."""
        self._add_bank(_ts(2026, 5, 22, 10, 0), 1400.0)  # prev
        self._add_bank(_ts(2026, 5, 25, 5, 0), 1408.0)   # 공휴일 change (KST 14:00)
        with self.Session() as db:
            action, row, code = W.process_date(db, HOLIDAY)
        self.assertEqual(action, "write")
        self.assertEqual(row["metadata_json"]["calendar_class"], "holiday")
        self.assertEqual(row["close"], Decimal("1408.0"))

    def test_weekend_no_changes_skip_ok(self):
        """주말 + changes 0 → skip_ok (weekend_no_changes, 정상)."""
        self._add_bank(_ts(2026, 5, 29, 10, 0), 1400.0)  # prev (Friday) only
        with self.Session() as db:
            action, row, code = W.process_date(db, SUNDAY)
        self.assertEqual(action, "skip_ok")
        self.assertEqual(code, "weekend_no_changes")
        self.assertIsNone(row)

    def test_weekend_changes_present_write(self):
        """주말 + changes → write (실 변동), calendar_class=weekend."""
        self._add_bank(_ts(2026, 5, 29, 10, 0), 1410.0)  # prev
        self._add_bank(_ts(2026, 5, 30, 2, 0), 1412.0)   # weekend change = close
        with self.Session() as db:
            action, row, code = W.process_date(db, SATURDAY)
        self.assertEqual(action, "write")
        self.assertEqual(row["metadata_json"]["calendar_class"], "weekend")
        self.assertEqual(row["close"], Decimal("1412.0"))


# ---------------------------------------------------------------------------
# Group B — write_with_transaction (commit / idempotent / overlap 안전망)
# ---------------------------------------------------------------------------

class TestWriteTransaction(_BaseDBTest):

    def _build_row(self, target=WEEKDAY):
        self._add_bank(_ts(2026, 5, 27, 10, 0), 1400.0)
        self._add_bank(_ts(2026, 5, 28, 5, 0), 1405.0)
        with self.Session() as db:
            action, row, _ = W.process_date(db, target)
        self.assertEqual(action, "write")
        return row

    def test_write_commits(self):
        row = self._build_row()
        success, issues = W.write_with_transaction_observed_eod([row])
        self.assertTrue(success, msg=str(issues))
        rows = self._daily_rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.date_kst, WEEKDAY)
        self.assertEqual(r.close, Decimal("1405.000000"))
        self.assertEqual(r.rate, r.close)
        self.assertEqual(r.close_basis, "hana_observed_eod")
        self.assertEqual(r.ohlc_quality, "observed_rollup")
        self.assertIsNone(r.basis_date)
        self.assertIsNone(r.published_at)

    def test_idempotent_upsert(self):
        row = self._build_row()
        W.write_with_transaction_observed_eod([row])
        first = [(r.date_kst, r.close, r.high, r.low) for r in self._daily_rows()]
        # rerun (same row)
        success, issues = W.write_with_transaction_observed_eod([row])
        self.assertTrue(success, msg=str(issues))
        second = [(r.date_kst, r.close, r.high, r.low) for r in self._daily_rows()]
        self.assertEqual(len(second), 1)             # 여전히 1 row
        self.assertEqual(first, second)              # 동일 값

    def test_official_overlap_rollback_safety(self):
        """기존 official_historical row와 overlap 시 nullable leftover → post-write
        nullable validation fail → rollback → official row 보존 (corruption 차단 안전망).

        ADR-034 §10 Open (close_basis 전환 정책 미확정) 전까지 overlap write 차단 근거.
        """
        # 1. 기존 official row 적재 (basis_date / published_at non-null)
        from app.source_daily_rates import upsert as upsert_fn
        with self.Session() as db:
            upsert_fn(
                db, commit=True,
                source="hana", asset="usd-krw", date_kst=WEEKDAY,
                close=1402.0, high=1402.0, low=1402.0,
                ohlc_quality="close_only",
                close_basis="hana_official_historical_backfill",
                source_method="external_backfill",
                basis_date=WEEKDAY,
                published_at=datetime(2026, 5, 29, 7, 46, 40),
                metadata_json={"pbldSqn": 1081},
            )
        # 2. observed_eod write 시도 (같은 date_kst)
        row = self._build_row()
        success, issues = W.write_with_transaction_observed_eod([row])
        # 3. post-write nullable validation fail → rollback
        self.assertFalse(success)
        self.assertTrue(
            any("basis_date NOT NULL" in i or "published_at NOT NULL" in i for i in issues),
            msg=f"expected nullable leftover issue, got {issues}",
        )
        # 4. official row 보존 (rollback)
        rows = self._daily_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].close_basis, "hana_official_historical_backfill")
        self.assertEqual(rows[0].close, Decimal("1402.000000"))

    def test_metadata_missing_key_rollback(self):
        """[N1 fail-closed] persisted metadata 필수 key 누락 → post-write fail → rollback.

        build_observed_eod_row는 항상 전체 key를 set하므로 현실 경로는 아니나,
        fail-closed 방어 속성을 잠금 (이전 fail-open 회귀 차단).
        """
        row = self._build_row()
        del row["metadata_json"]["rollup_point_count"]  # 필수 key 제거
        success, issues = W.write_with_transaction_observed_eod([row])
        self.assertFalse(success)
        self.assertTrue(
            any("metadata 필수 key 누락" in i for i in issues),
            msg=f"expected missing-key issue, got {issues}",
        )
        self.assertEqual(len(self._daily_rows()), 0)  # rollback (미적재)


# ---------------------------------------------------------------------------
# Group D — production write guard (non-SQLite reject)
# ---------------------------------------------------------------------------

class TestProductionGuard(unittest.TestCase):
    """check_production_write_guard — non-SQLite dialect는 --allow-production-write 필수.

    production write 사고 방지 핵심 안전장치. MagicMock으로 dialect 주입 (DBAPI import 회피).
    """

    def _fake_pg_engine(self):
        fake = MagicMock()
        fake.url.get_dialect.return_value.name = "postgresql"
        fake.url.host = "rds-host"
        return fake

    def test_rejects_non_sqlite_without_allow(self):
        """non-SQLite + --allow-production-write 없음 → 차단 (error message)."""
        with patch("app.database.engine", self._fake_pg_engine()):
            err = W.check_production_write_guard(allow_production=False)
        self.assertIsNotNone(err)
        self.assertIn("postgresql", err)
        self.assertIn("***", err)  # host redacted (보안 원칙)

    def test_allows_non_sqlite_with_explicit_flag(self):
        """non-SQLite + --allow-production-write 명시 → 통과 (None)."""
        with patch("app.database.engine", self._fake_pg_engine()):
            err = W.check_production_write_guard(allow_production=True)
        self.assertIsNone(err)

    def test_sqlite_always_passes(self):
        """SQLite는 allow 무관 통과 (local smoke 허용)."""
        fake = MagicMock()
        fake.url.get_dialect.return_value.name = "sqlite"
        with patch("app.database.engine", fake):
            self.assertIsNone(W.check_production_write_guard(allow_production=False))


# ---------------------------------------------------------------------------
# Group C — _run_write 종료 코드 (B1 atomicity / B2 negative reject)
# ---------------------------------------------------------------------------

class TestRunWriteExitCodes(_BaseDBTest):

    def _args(self, start, end, include_today=False, allow_production=False,
              emit_verdict=False):
        return argparse.Namespace(
            start_date=start, end_date=end,
            include_today=include_today, allow_production_write=allow_production,
            emit_daily_append_verdict=emit_verdict,
        )

    def _run(self, args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = W._run_write(args)
        return rc, buf.getvalue()

    def test_mixed_range_atomicity_no_partial_commit(self):
        """[B1] 평일 무변동이 range에 섞이면 transaction 전 abort → 0 row (부분 commit 금지)."""
        self._add_bank(_ts(2026, 5, 26, 10, 0), 1395.0)  # prev for 5/27
        self._add_bank(_ts(2026, 5, 27, 5, 0), 1400.0)   # 5/27 change (write 후보)
        # 5/28 window에는 change 없음 → weekday_no_changes
        rc, out = self._run(self._args(date(2026, 5, 27), date(2026, 5, 28)))
        self.assertEqual(rc, 1)
        self.assertEqual(len(self._daily_rows()), 0)      # 5/27도 미적재 (atomicity)
        self.assertIn("atomicity", out)

    def test_negative_raw_rejected(self):
        """[B2] 음수 raw rate → pre-write validation abort → 0 row (exit 1)."""
        self._add_bank(_ts(2026, 5, 27, 10, 0), -2.0)    # prev (음수)
        self._add_bank(_ts(2026, 5, 28, 5, 0), -1.0)     # change (음수)
        rc, out = self._run(self._args(WEEKDAY, WEEKDAY))
        self.assertEqual(rc, 1)
        self.assertEqual(len(self._daily_rows()), 0)
        self.assertIn("non-positive", out)

    def test_clean_weekday_write_exit_0(self):
        """평일 정상 changes → write 1 row, exit 0."""
        self._add_bank(_ts(2026, 5, 27, 10, 0), 1400.0)
        self._add_bank(_ts(2026, 5, 28, 5, 0), 1405.0)
        rc, out = self._run(self._args(WEEKDAY, WEEKDAY))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._daily_rows()), 1)

    # ── verdict sentinel (--emit-daily-append-verdict) ──

    def _extract_verdict(self, out):
        """출력에서 DAILY_APPEND_VERDICT_JSON sentinel 정확히 1개 추출."""
        import json as _json
        prefix = W.SENTINEL_PREFIX
        lines = [ln for ln in out.splitlines() if ln.startswith(prefix)]
        self.assertEqual(len(lines), 1, msg=f"expected exactly 1 sentinel, got {len(lines)}")
        return _json.loads(lines[0][len(prefix):])

    def test_verdict_written(self):
        self._add_bank(_ts(2026, 5, 27, 10, 0), 1400.0)
        self._add_bank(_ts(2026, 5, 28, 5, 0), 1405.0)
        rc, out = self._run(self._args(WEEKDAY, WEEKDAY, emit_verdict=True))
        self.assertEqual(rc, 0)
        v = self._extract_verdict(out)
        self.assertEqual(v["version"], 1)
        self.assertEqual(v["source"], "hana")
        self.assertEqual(v["date_kst"], "2026-05-28")
        self.assertEqual(v["status"], "written")
        self.assertEqual(v["rows"], 1)

    def test_verdict_skipped_weekend(self):
        # SATURDAY(과거 주말) 사용 — SUNDAY는 오늘(2026-05-31)이라 today 가드에 걸림
        rc, out = self._run(self._args(SATURDAY, SATURDAY, emit_verdict=True))
        self.assertEqual(rc, 0)
        v = self._extract_verdict(out)
        self.assertEqual(v["status"], "skipped")
        self.assertEqual(v["reason"], "weekend_no_changes")
        self.assertEqual(v["rows"], 0)

    def test_verdict_skipped_holiday(self):
        rc, out = self._run(self._args(HOLIDAY, HOLIDAY, emit_verdict=True))
        self.assertEqual(rc, 0)
        v = self._extract_verdict(out)
        self.assertEqual(v["status"], "skipped")
        self.assertEqual(v["reason"], "holiday_no_changes")
        self.assertEqual(v["rows"], 0)

    def test_verdict_error_business_day(self):
        self._add_bank(_ts(2026, 5, 27, 10, 0), 1400.0)  # prev only, no changes
        rc, out = self._run(self._args(WEEKDAY, WEEKDAY, emit_verdict=True))
        self.assertEqual(rc, 1)
        v = self._extract_verdict(out)
        self.assertEqual(v["status"], "error")
        self.assertEqual(v["reason"], "business_day_no_changes")
        self.assertEqual(v["rows"], 0)


if __name__ == "__main__":
    unittest.main()
