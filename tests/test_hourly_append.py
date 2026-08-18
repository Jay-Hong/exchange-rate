"""Bithumb hourly append PLAN 단위 테스트 (ADR-035 D3 — going-forward freshness).

`scripts/hourly_append_source_hourly_rates.py`의 window 계산 + compute_append_plan을
in-memory SQLite/synthetic으로 검증. read-only(write 0) plan만. 외부 의존성 0.

검증 포인트:
- previous_complete_hour: 현재 incomplete hour 제외 (day boundary 포함)
- kst_naive_to_utc_naive 경계
- compute_append_plan: inserted / updated_same(완전 동일) / updated_changed(price) / updated_metadata_changed(late tick, price 무변) / prune 분류
"""
from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import models  # noqa: E402
from app.models import SourceHourlyRate, SourceRate  # noqa: E402
import backfill_bithumb_source_hourly_rates as B  # noqa: E402
import hourly_append_source_hourly_rates as A  # noqa: E402


class TestWindow(unittest.TestCase):
    def test_previous_complete_hour(self):
        # 14:30 → 현재 14:00(incomplete) 제외 → 직전 완료 13:00
        self.assertEqual(A.previous_complete_hour(datetime(2026, 6, 8, 14, 30)),
                         datetime(2026, 6, 8, 13, 0))
        # 00:05 → day boundary → 전날 23:00
        self.assertEqual(A.previous_complete_hour(datetime(2026, 6, 8, 0, 5)),
                         datetime(2026, 6, 7, 23, 0))

    def test_kst_naive_to_utc_naive(self):
        # KST 14:00 = UTC 05:00
        self.assertEqual(A.kst_naive_to_utc_naive(datetime(2026, 6, 8, 14, 0)),
                         datetime(2026, 6, 8, 5, 0))


class TestComputeAppendPlan(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def _tick(self, utc_dt, rate):
        self.db.add(SourceRate(source="bithumb", asset="usdt-krw", rate=rate, timestamp=utc_dt))

    def _bucket(self, kst_dt, close, metadata=None):
        self.db.add(SourceHourlyRate(
            source="bithumb", asset="usdt-krw", bucket_ts_kst=kst_dt,
            rate=Decimal(str(close)), close=Decimal(str(close)),
            high=Decimal(str(close)), low=Decimal(str(close)),
            ohlc_quality="observed_rollup", close_basis="bithumb_observed_hourly",
            source_method="observed_rollup", metadata_json=metadata))

    def test_inserted_updated_same_prune(self):
        now = datetime(2026, 6, 8, 14, 30)  # 직전 완료 13:00
        # candidate: KST 12:30(bucket 12:00) 1500, KST 13:30(bucket 13:00) 1510
        self._tick(datetime(2026, 6, 8, 3, 30), 1500.0)   # UTC 03:30 = KST 12:30
        self._tick(datetime(2026, 6, 8, 4, 30), 1510.0)   # UTC 04:30 = KST 13:30
        # 기존 12:00 = candidate와 price + metadata 완전 동일 → updated_same (진짜 no-op)
        cand_12 = B.rollup_ticks_to_hourly([(1500.0, datetime(2026, 6, 8, 3, 30))])[0]
        self._bucket(datetime(2026, 6, 8, 12, 0), 1500.0, metadata=cand_12["metadata_json"])
        self._bucket(datetime(2026, 5, 20, 0, 0), 1400.0)  # 14d+ old → prune
        self.db.commit()

        plan = A.compute_append_plan(self.db, now, window_days=2, retention_days=14)
        self.assertEqual(plan.window_end, datetime(2026, 6, 8, 13, 0))  # 직전 완료 hour
        self.assertEqual(plan.candidate_count, 2)
        self.assertEqual(plan.inserted, 1)                  # 13:00 신규
        self.assertEqual(plan.updated_same, 1)              # 12:00 완전 동일
        self.assertEqual(plan.updated_metadata_changed, 0)
        self.assertEqual(plan.updated_changed, 0)
        self.assertEqual(plan.prune_count, 1)               # 05-20 < cutoff(05-25 14:00)

    def test_updated_changed_price(self):
        now = datetime(2026, 6, 8, 14, 30)
        self._tick(datetime(2026, 6, 8, 4, 30), 1510.0)    # bucket 13:00, close 1510
        self._bucket(datetime(2026, 6, 8, 13, 0), 1505.0)  # 기존 13:00 다른 price → updated_changed
        self.db.commit()

        plan = A.compute_append_plan(self.db, now, window_days=2, retention_days=14)
        self.assertEqual(plan.inserted, 0)
        self.assertEqual(plan.updated_same, 0)
        self.assertEqual(plan.updated_metadata_changed, 0)
        self.assertEqual(plan.updated_changed, 1)           # price revise
        self.assertEqual(plan.changed_buckets, [datetime(2026, 6, 8, 13, 0)])

    def test_updated_metadata_changed(self):
        # price OHLC는 동일하지만 metadata(point_count 등)만 변동 → metadata_changed (no-op 아님)
        now = datetime(2026, 6, 8, 14, 30)
        self._tick(datetime(2026, 6, 8, 4, 30), 1510.0)    # bucket 13:00, candidate point_count=1
        self._bucket(datetime(2026, 6, 8, 13, 0), 1510.0,  # 같은 price + 다른 metadata
                     metadata={"point_count": 99, "first_ts_kst": "x", "last_ts_kst": "x"})
        self.db.commit()

        plan = A.compute_append_plan(self.db, now, window_days=2, retention_days=14)
        self.assertEqual(plan.updated_changed, 0)
        self.assertEqual(plan.updated_same, 0)
        self.assertEqual(plan.updated_metadata_changed, 1)  # late tick, price 무변

    def test_current_incomplete_hour_excluded(self):
        now = datetime(2026, 6, 8, 14, 30)
        # 현재 incomplete hour(14:00) tick은 plan에서 제외돼야 함
        self._tick(datetime(2026, 6, 8, 5, 30), 1520.0)   # UTC 05:30 = KST 14:30 (현재 hour)
        self._tick(datetime(2026, 6, 8, 4, 30), 1510.0)   # KST 13:30 (직전 완료)
        self.db.commit()

        plan = A.compute_append_plan(self.db, now, window_days=2, retention_days=14)
        self.assertEqual(plan.candidate_count, 1)         # 13:00만 (14:00 제외)
        self.assertEqual(plan.latest_bucket_ts, datetime(2026, 6, 8, 13, 0))


class TestWriteHelpers(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def _bucket(self, kst_dt, close):
        self.db.add(SourceHourlyRate(
            source="bithumb", asset="usdt-krw", bucket_ts_kst=kst_dt,
            rate=Decimal(str(close)), close=Decimal(str(close)),
            high=Decimal(str(close)), low=Decimal(str(close)),
            ohlc_quality="observed_rollup", close_basis="bithumb_observed_hourly",
            source_method="observed_rollup"))

    def test_retention_cutoff_kst(self):
        # now 14:30 → floor 14:00 - 14d = 2026-05-25 14:00
        self.assertEqual(A.retention_cutoff_kst(datetime(2026, 6, 8, 14, 30), 14),
                         datetime(2026, 5, 25, 14, 0))

    def test_prune_old_buckets(self):
        self._bucket(datetime(2026, 5, 20, 0, 0), 1400.0)   # < cutoff → prune
        self._bucket(datetime(2026, 5, 25, 14, 0), 1450.0)  # == cutoff → 유지 (< 아님)
        self._bucket(datetime(2026, 6, 8, 13, 0), 1500.0)   # > cutoff → 유지
        self.db.commit()
        deleted = A.prune_old_buckets(self.db, datetime(2026, 5, 25, 14, 0))
        self.assertEqual(deleted, 1)
        self.assertEqual(self.db.query(SourceHourlyRate).count(), 2)

    def test_fetch_candidates_excludes_current_hour(self):
        # KST 13:30(bucket 13:00) + 14:30(현재 incomplete) — now 14:30 → 직전 완료 13:00만
        self.db.add(SourceRate(source="bithumb", asset="usdt-krw", rate=1510.0,
                               timestamp=datetime(2026, 6, 8, 4, 30)))   # KST 13:30
        self.db.add(SourceRate(source="bithumb", asset="usdt-krw", rate=1520.0,
                               timestamp=datetime(2026, 6, 8, 5, 30)))   # KST 14:30 (현재 hour)
        self.db.commit()
        window_start, prev_complete, candidates = A._fetch_candidates(
            self.db, datetime(2026, 6, 8, 14, 30), 2)
        self.assertEqual(prev_complete, datetime(2026, 6, 8, 13, 0))
        self.assertEqual(len(candidates), 1)                  # 14:00 제외
        self.assertEqual(candidates[0]["bucket_ts_kst"], datetime(2026, 6, 8, 13, 0))


class TestCli(unittest.TestCase):
    def test_as_of_write_guard_contract(self):
        self.assertIsNone(A.validate_write_time_override(False, "2026-08-21T12:00"))
        self.assertIsNone(A.validate_write_time_override(True, None))
        self.assertIsNotNone(
            A.validate_write_time_override(True, "2026-08-21T12:00")
        )

    def test_cli_rejects_as_of_write(self):
        import os
        import subprocess
        import tempfile
        script = str(Path(__file__).resolve().parent.parent / "scripts"
                     / "hourly_append_source_hourly_rates.py")
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env["DATABASE_URL"] = f"sqlite:///{Path(temp_dir) / 'guard.db'}"
            r = subprocess.run(
                [sys.executable, script, "--write", "--allow-production-write",
                 "--as-of", "2026-08-21T12:00"],
                capture_output=True,
                text=True,
                env=env,
            )
        self.assertEqual(r.returncode, 2)
        self.assertIn("--as-of는 PLAN 전용", r.stdout + r.stderr)

    def test_as_of_write_rejected_before_production_guard(self):
        stdout = StringIO()
        argv = [
            "hourly_append_source_hourly_rates.py",
            "--write",
            "--allow-production-write",
            "--as-of",
            "2026-08-21T12:00",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(A.B, "check_production_write_guard") as production_guard,
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            A.main()
        self.assertEqual(raised.exception.code, 2)
        production_guard.assert_not_called()

    def test_cron_write_without_as_of_reaches_production_guard(self):
        stdout = StringIO()
        argv = [
            "hourly_append_source_hourly_rates.py",
            "--write",
            "--allow-production-write",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(
                A.B,
                "check_production_write_guard",
                return_value="test production guard stop",
            ) as production_guard,
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            A.main()
        self.assertEqual(raised.exception.code, 2)
        production_guard.assert_called_once_with(True)
        self.assertIn("test production guard stop", stdout.getvalue())

    def test_plan_as_of_reaches_read_path(self):
        argv = [
            "hourly_append_source_hourly_rates.py",
            "--as-of",
            "2026-08-21T12:00",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("app.database.SessionLocal"),
            patch.object(
                A,
                "_fetch_candidates",
                side_effect=RuntimeError("read path reached"),
            ) as fetch_candidates,
            self.assertRaisesRegex(RuntimeError, "read path reached"),
        ):
            A.main()
        fetch_candidates.assert_called_once()
        self.assertEqual(
            fetch_candidates.call_args.args[1],
            datetime(2026, 8, 21, 12, 0),
        )

    def test_window_days_guard(self):
        import os
        import subprocess
        import tempfile
        script = str(Path(__file__).resolve().parent.parent / "scripts"
                     / "hourly_append_source_hourly_rates.py")
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env["DATABASE_URL"] = f"sqlite:///{Path(temp_dir) / 'guard.db'}"
            r = subprocess.run(
                [sys.executable, script, "--window-days", "0"],
                capture_output=True,
                text=True,
                env=env,
            )
        self.assertEqual(r.returncode, 2)          # DB 연결 전 fail-close
        self.assertIn("window-days", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
