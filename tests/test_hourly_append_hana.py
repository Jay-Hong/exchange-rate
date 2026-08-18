"""Hana per-currency hourly append 단위 테스트 (ADR-035 D3 — PLAN classification + atomic prune).

`scripts/hourly_append_hana_source_hourly_rates.py`의 fetch_candidates_hana +
compute_append_plan_hana(classification incl ohlc_quality 전이) + prune_old_buckets_hana(atomic)을
in-memory SQLite/synthetic으로 검증. 외부 의존성 0.

검증 포인트:
- compute_append_plan_hana: inserted / updated_same / updated_changed(price) / updated_metadata_changed
- ohlc_quality 전이: close_only→observed_rollup (late tick) → updated_metadata_changed
- ohlc_quality-only 변화(price+metadata 동일): defense-in-depth로 updated_metadata_changed 잡음
- prune_old_buckets_hana: atomic (subset isolation + multi-asset)
- currency 격리
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
from app.models import BankExchangeRate, SourceHourlyRate  # noqa: E402
import backfill_hana_source_hourly_rates as HV  # noqa: E402
import hourly_append_hana_source_hourly_rates as AH  # noqa: E402


class TestComputeAppendPlanHana(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def _tick(self, utc_dt, rate, asset):
        self.db.add(BankExchangeRate(bank="hana", currency=asset, rate=rate, timestamp=utc_dt))

    def _bucket(self, kst_dt, asset, close, ohlc_quality="observed_rollup", metadata=None):
        self.db.add(SourceHourlyRate(
            source="hana", asset=asset, bucket_ts_kst=kst_dt,
            rate=Decimal(str(close)), close=Decimal(str(close)),
            high=Decimal(str(close)), low=Decimal(str(close)),
            ohlc_quality=ohlc_quality, close_basis="hana_observed_hourly",
            source_method="observed_rollup", metadata_json=metadata))

    def _plan(self, now, asset, window_days=2, retention_days=14):
        ws, pc, cand = AH.fetch_candidates_hana(self.db, now, window_days, asset)
        return AH.compute_append_plan_hana(self.db, asset, cand, ws, pc, now, retention_days)

    def test_inserted_updated_same_prune(self):
        now = datetime(2026, 6, 8, 14, 30)  # 직전 완료 13:00
        self._tick(datetime(2026, 6, 8, 3, 30), 1380.0, "usd-krw")   # KST 12:30 single → close_only bucket 12:00
        self._tick(datetime(2026, 6, 8, 4, 30), 1383.0, "usd-krw")   # KST 13:30 → bucket 13:00
        cand_12 = HV.rollup_to_hourly([(1380.0, datetime(2026, 6, 8, 3, 30))], "usd-krw")[0]
        self._bucket(datetime(2026, 6, 8, 12, 0), "usd-krw", 1380.0,
                     ohlc_quality="close_only", metadata=cand_12["metadata_json"])   # 완전 동일
        self._bucket(datetime(2026, 5, 20, 0, 0), "usd-krw", 1400.0)                  # 14d+ → prune
        self.db.commit()

        plan = self._plan(now, "usd-krw")
        self.assertEqual(plan.window_end, datetime(2026, 6, 8, 13, 0))
        self.assertEqual(plan.candidate_count, 2)
        self.assertEqual(plan.inserted, 1)                  # 13:00 신규
        self.assertEqual(plan.updated_same, 1)              # 12:00 close_only 완전 동일
        self.assertEqual(plan.updated_changed, 0)
        self.assertEqual(plan.updated_metadata_changed, 0)
        self.assertEqual(plan.prune_count, 1)               # 05-20

    def test_updated_changed_price(self):
        now = datetime(2026, 6, 8, 14, 30)
        self._tick(datetime(2026, 6, 8, 4, 30), 1383.0, "usd-krw")    # bucket 13:00 close 1383
        self._bucket(datetime(2026, 6, 8, 13, 0), "usd-krw", 1380.0)  # 기존 다른 price
        self.db.commit()

        plan = self._plan(now, "usd-krw")
        self.assertEqual(plan.updated_changed, 1)
        self.assertEqual(plan.changed_buckets, [datetime(2026, 6, 8, 13, 0)])

    def test_close_only_to_observed_rollup(self):
        # late tick으로 single-tick(close_only) → multi-tick(observed_rollup), 같은 값이라 price 동일
        now = datetime(2026, 6, 8, 14, 30)
        self._tick(datetime(2026, 6, 8, 4, 30), 1380.0, "usd-krw")    # KST 13:30
        self._tick(datetime(2026, 6, 8, 4, 50), 1380.0, "usd-krw")    # KST 13:50 (2번째, 같은 값 → candidate observed_rollup)
        self._bucket(datetime(2026, 6, 8, 13, 0), "usd-krw", 1380.0,  # 기존 close_only point_count=1
                     ohlc_quality="close_only",
                     metadata={"point_count": 1, "first_ts_kst": "x", "last_ts_kst": "x"})
        self.db.commit()

        plan = self._plan(now, "usd-krw")
        self.assertEqual(plan.updated_changed, 0)           # price 동일 (1380)
        self.assertEqual(plan.updated_same, 0)
        self.assertEqual(plan.updated_metadata_changed, 1)  # point_count 1→2 + ohlc_quality close_only→observed_rollup

    def test_ohlc_quality_only_change_flagged(self):
        # price+metadata 동일인데 ohlc_quality만 다른 (인위적 corrupt) → ohlc_quality 비교가 defense-in-depth로 잡음
        now = datetime(2026, 6, 8, 14, 30)
        md = {"point_count": 2, "first_ts_kst": "2026-06-08T13:10:00+09:00",
              "last_ts_kst": "2026-06-08T13:50:00+09:00", "source_table": "bank_exchange_rates"}
        self._bucket(datetime(2026, 6, 8, 13, 0), "usd-krw", 1380.0,
                     ohlc_quality="observed_rollup", metadata=md)
        self.db.commit()
        cand = [{"bucket_ts_kst": datetime(2026, 6, 8, 13, 0), "source": "hana", "asset": "usd-krw",
                 "rate": Decimal("1380.0"), "close": Decimal("1380.0"),
                 "high": Decimal("1380.0"), "low": Decimal("1380.0"),
                 "ohlc_quality": "close_only",   # price+metadata 동일, ohlc_quality만 다름
                 "close_basis": "hana_observed_hourly", "source_method": "observed_rollup",
                 "metadata_json": md}]
        plan = AH.compute_append_plan_hana(
            self.db, "usd-krw", cand,
            datetime(2026, 6, 7, 13, 0), datetime(2026, 6, 8, 13, 0), now, 14)
        self.assertEqual(plan.updated_changed, 0)
        self.assertEqual(plan.updated_same, 0)
        self.assertEqual(plan.updated_metadata_changed, 1)   # ohlc_quality 비교가 잡음 (metadata 동일이어도)

    def test_currency_isolation(self):
        now = datetime(2026, 6, 8, 14, 30)
        self._tick(datetime(2026, 6, 8, 4, 30), 1380.0, "usd-krw")   # usd KST 13:30
        self._tick(datetime(2026, 6, 8, 4, 30), 900.0, "jpy-krw")    # jpy KST 13:30
        self._bucket(datetime(2026, 5, 20, 0, 0), "jpy-krw", 880.0)  # jpy old (usd prune 무관)
        self.db.commit()

        plan = self._plan(now, "usd-krw")
        self.assertEqual(plan.candidate_count, 1)           # usd 13:00만
        self.assertEqual(plan.prune_count, 0)               # usd엔 old 없음

    def test_prune_atomic_subset_isolation(self):
        # prune은 asset.in_(assets) — assets에 없는 통화 old는 안 건드림
        self._bucket(datetime(2026, 5, 20, 0, 0), "usd-krw", 1380.0)   # usd old → prune
        self._bucket(datetime(2026, 6, 8, 13, 0), "usd-krw", 1382.0)   # usd recent → 유지
        self._bucket(datetime(2026, 5, 20, 0, 0), "jpy-krw", 900.0)    # jpy old (assets=[usd]엔 무관)
        self.db.commit()
        deleted = AH.prune_old_buckets_hana(self.db, ["usd-krw"], datetime(2026, 5, 25, 14, 0))
        self.assertEqual(deleted, 1)                                                                  # usd old만
        self.assertEqual(self.db.query(SourceHourlyRate).filter(SourceHourlyRate.asset == "jpy-krw").count(), 1)  # jpy 격리
        self.assertEqual(self.db.query(SourceHourlyRate).filter(SourceHourlyRate.asset == "usd-krw").count(), 1)  # usd recent 유지

    def test_prune_atomic_multi_asset(self):
        # 여러 통화 old를 한 transaction에서 atomic 삭제
        for a, r in [("usd-krw", 1380.0), ("jpy-krw", 900.0), ("eur-krw", 1500.0)]:
            self._bucket(datetime(2026, 5, 20, 0, 0), a, r)            # 3통화 old
        self._bucket(datetime(2026, 6, 8, 13, 0), "usd-krw", 1382.0)  # recent → 유지
        self.db.commit()
        deleted = AH.prune_old_buckets_hana(
            self.db, ["usd-krw", "jpy-krw", "eur-krw"], datetime(2026, 5, 25, 14, 0))
        self.assertEqual(deleted, 3)                                   # usd/jpy/eur old atomic
        self.assertEqual(self.db.query(SourceHourlyRate).count(), 1)   # recent usd만


class TestCli(unittest.TestCase):
    def test_cli_rejects_as_of_write(self):
        import os
        import subprocess
        import tempfile
        script = str(Path(__file__).resolve().parent.parent / "scripts"
                     / "hourly_append_hana_source_hourly_rates.py")
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
            "hourly_append_hana_source_hourly_rates.py",
            "--currency",
            "all",
            "--write",
            "--allow-production-write",
            "--as-of",
            "2026-08-21T12:00",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(AH.B, "check_production_write_guard") as production_guard,
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            AH.main()
        self.assertEqual(raised.exception.code, 2)
        production_guard.assert_not_called()

    def test_cron_write_without_as_of_reaches_production_guard(self):
        stdout = StringIO()
        argv = [
            "hourly_append_hana_source_hourly_rates.py",
            "--currency",
            "all",
            "--write",
            "--allow-production-write",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(
                AH.B,
                "check_production_write_guard",
                return_value="test production guard stop",
            ) as production_guard,
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            AH.main()
        self.assertEqual(raised.exception.code, 2)
        production_guard.assert_called_once_with(True)
        self.assertIn("test production guard stop", stdout.getvalue())

    def test_plan_as_of_reaches_read_path(self):
        argv = [
            "hourly_append_hana_source_hourly_rates.py",
            "--currency",
            "all",
            "--as-of",
            "2026-08-21T12:00",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("app.database.SessionLocal"),
            patch.object(
                AH,
                "fetch_candidates_hana",
                side_effect=RuntimeError("read path reached"),
            ) as fetch_candidates,
            self.assertRaisesRegex(RuntimeError, "read path reached"),
        ):
            AH.main()
        fetch_candidates.assert_called_once()
        self.assertEqual(
            fetch_candidates.call_args.args[1],
            datetime(2026, 8, 21, 12, 0),
        )


if __name__ == "__main__":
    unittest.main()
