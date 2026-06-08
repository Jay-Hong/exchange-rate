"""KRX hourly-append in-process hook 단위 테스트 (ADR-035 D3).

두 layer 검증:
  1. app.krx_hourly.append_krx_cf_hourly_rows — in-memory SQLite로 transaction + prune + contract
     source(source_daily_rates 재사용) 직접 검증 (case b/c/d/f).
  2. KrxCloseWindowWriter._sync_write hook slot — gate(env off / daily action) + 격리
     (hourly 예외가 close finalizer return True를 안 깨뜨림) (case a/e).

KST = UTC + 9 (source_rates.timestamp는 UTC naive). 예: KST 09:15 = UTC 00:15 / KST 15:45 = UTC 06:45.
외부 의존성 0 (mock + in-memory level).
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import config  # noqa: E402
from app import krx_hourly as KH  # noqa: E402
from app import models  # noqa: E402
from app.models import SourceDailyRate, SourceHourlyRate  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


# ═════════════════════════════════════════════════════════════════════════════
# Layer 1 — append_krx_cf_hourly_rows (transaction + contract source + prune)
# ═════════════════════════════════════════════════════════════════════════════

class TestAppendKrxCfHourlyRows(unittest.TestCase):
    """in-memory SQLite. source_rates(tick) + source_daily_rates(contract) seed → append helper."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def _seed_daily(self, db, date_kst=date(2026, 6, 8), contract="A75606"):
        """source_daily_rates KRX row (hourly hook이 contract_code resolve 시 재사용)."""
        db.add(SourceDailyRate(
            source="krx", asset="usd-krw-futures", date_kst=date_kst,
            rate=1500, close=1500, ohlc_quality="source_ohlc",
            close_basis="krx_cf_close_1545", source_method="krx_openapi_daily",
            contract_code=contract,
        ))
        db.commit()

    def _seed_ticks(self, db, ticks):
        """source_rates KRX tick seed. ticks = [(rate, utc_naive_datetime), ...]."""
        from app.models import SourceRate
        for rate, ts in ticks:
            db.add(SourceRate(source="krx", asset="usd-krw-futures", rate=rate, timestamp=ts))
        db.commit()

    def _cf_day_ticks(self):
        """2026-06-08 CF 세션 8 hour bucket 분포 (08~15) + CM 야간 1 tick(drop)."""
        # KST hour h → UTC (h-9). 08:30 KST = 2026-06-07 23:30 UTC.
        ticks = [
            (1500.0, datetime(2026, 6, 7, 23, 30)),   # KST 08:30 → 08:00 bucket (경계 inclusive)
            (1501.0, datetime(2026, 6, 8, 0, 15)),    # KST 09:15 → 09:00
            (1502.0, datetime(2026, 6, 8, 1, 15)),    # KST 10:15 → 10:00
            (1503.0, datetime(2026, 6, 8, 2, 15)),    # KST 11:15 → 11:00
            (1504.0, datetime(2026, 6, 8, 3, 15)),    # KST 12:15 → 12:00
            (1505.0, datetime(2026, 6, 8, 4, 15)),    # KST 13:15 → 13:00
            (1506.0, datetime(2026, 6, 8, 5, 15)),    # KST 14:15 → 14:00
            (1507.0, datetime(2026, 6, 8, 6, 30)),    # KST 15:30 → 15:00
            (1508.0, datetime(2026, 6, 8, 6, 45)),    # KST 15:45 → 15:00 (close, inclusive)
            (1490.0, datetime(2026, 6, 8, 10, 0)),    # KST 19:00 CM 야간 → drop
        ]
        return ticks

    def _hourly_rows(self):
        db = self.Session()
        try:
            return (db.query(SourceHourlyRate)
                    .filter(SourceHourlyRate.source == "krx")
                    .order_by(SourceHourlyRate.bucket_ts_kst).all())
        finally:
            db.close()

    def test_daily_insert_eight_cf_buckets_with_contract(self):
        """(b) daily row 존재 → hourly 8 bucket upsert, contract+session persisted, 15:00 bucket=15:45 close."""
        db = self.Session()
        self._seed_daily(db)
        self._seed_ticks(db, self._cf_day_ticks())
        counts = KH.append_krx_cf_hourly_rows(db, date(2026, 6, 8))
        db.close()
        self.assertEqual(counts["buckets"], 8)             # CF 08~15 = 8 (CM drop)
        self.assertEqual(counts["inserted"], 8)
        self.assertEqual(counts["updated"], 0)
        written = self._hourly_rows()
        self.assertEqual([w.bucket_ts_kst.hour for w in written], [8, 9, 10, 11, 12, 13, 14, 15])
        self.assertTrue(all(w.contract_code == "A75606" for w in written))         # contract 재사용
        self.assertTrue(all((w.metadata_json or {}).get("session") == "CF" for w in written))
        self.assertTrue(all(w.rate == w.close for w in written))                   # invariant
        self.assertTrue(all(w.basis_date is None and w.published_at is None for w in written))
        b15 = [w for w in written if w.bucket_ts_kst.hour == 15][0]
        self.assertEqual(float(b15.close), 1508.0)                                 # 15:45 inclusive
        self.assertEqual(float(b15.high), 1508.0)
        self.assertEqual(float(b15.low), 1507.0)                                   # 15:30 tick

    def test_daily_skip_idempotent_reroll(self):
        """(c) daily SKIP path → hourly 재실행 idempotent (updated, not inserted, no side effect)."""
        db = self.Session()
        self._seed_daily(db)
        self._seed_ticks(db, self._cf_day_ticks())
        first = KH.append_krx_cf_hourly_rows(db, date(2026, 6, 8))
        self.assertEqual(first["inserted"], 8)
        # 재실행 (daily SKIP 시나리오와 동형 — 같은 date 다시 append)
        second = KH.append_krx_cf_hourly_rows(db, date(2026, 6, 8))
        db.close()
        self.assertEqual(second["inserted"], 0)            # 전부 기존 → updated
        self.assertEqual(second["updated"], 8)
        self.assertEqual(second["buckets"], 8)
        self.assertEqual(len(self._hourly_rows()), 8)      # 중복 row 0 (idempotent upsert)

    def test_contract_none_rollback(self):
        """contract None(daily row 부재) → post-write 검증 실패 → RuntimeError + rollback (0 row)."""
        db = self.Session()
        # daily seed 생략 → contract_map 빈 → 모든 bucket contract None
        self._seed_ticks(db, self._cf_day_ticks())
        with self.assertRaises(RuntimeError) as ctx:
            KH.append_krx_cf_hourly_rows(db, date(2026, 6, 8))
        db.close()
        self.assertIn("contract_code IS NULL", str(ctx.exception))
        self.assertEqual(len(self._hourly_rows()), 0)      # rollback — 0 row

    def test_no_ticks_no_op(self):
        """tick 0 (무거래/무관측) → write skip, prune 수행(대상 없으면 pruned=0), 빈 counts (fail 아님 — 정상 gap)."""
        db = self.Session()
        self._seed_daily(db)
        counts = KH.append_krx_cf_hourly_rows(db, date(2026, 6, 8))
        db.close()
        self.assertEqual(counts, {"inserted": 0, "updated": 0, "pruned": 0, "buckets": 0})
        self.assertEqual(len(self._hourly_rows()), 0)

    def test_prune_only_krx_other_source_preserved(self):
        """(f) retention prune은 KRX source/asset만 삭제 — 다른 source bucket 보존."""
        from app.source_hourly_rates import upsert as upsert_fn
        db = self.Session()
        self._seed_daily(db)
        self._seed_ticks(db, self._cf_day_ticks())
        # retention(14d) 밖 old bucket 2개: KRX(삭제 대상) + bithumb(보존 대상)
        old_ts = datetime.now(KST).replace(tzinfo=None, minute=0, second=0, microsecond=0) - timedelta(days=40)
        upsert_fn(db, source="krx", asset="usd-krw-futures", bucket_ts_kst=old_ts,
                  close=1400.0, high=1400.0, low=1400.0, ohlc_quality="observed_rollup",
                  close_basis="krx_observed_hourly", source_method="observed_rollup",
                  contract_code="A75600",
                  metadata_json={"point_count": 1, "first_ts_kst": "x", "last_ts_kst": "x", "session": "CF"})
        upsert_fn(db, source="bithumb", asset="usdt-krw", bucket_ts_kst=old_ts,
                  close=1300.0, high=1300.0, low=1300.0, ohlc_quality="observed_rollup",
                  close_basis="bithumb_observed_hourly", source_method="observed_rollup",
                  metadata_json={"point_count": 1, "first_ts_kst": "x", "last_ts_kst": "x"})
        counts = KH.append_krx_cf_hourly_rows(db, date(2026, 6, 8))
        db.close()
        self.assertEqual(counts["pruned"], 1)              # KRX old 1개만 prune
        # bithumb old bucket 보존 확인
        db2 = self.Session()
        try:
            bith = db2.query(SourceHourlyRate).filter(SourceHourlyRate.source == "bithumb").all()
            krx_old = (db2.query(SourceHourlyRate)
                       .filter(SourceHourlyRate.source == "krx",
                               SourceHourlyRate.bucket_ts_kst == old_ts).all())
        finally:
            db2.close()
        self.assertEqual(len(bith), 1)                     # 다른 source 보존
        self.assertEqual(len(krx_old), 0)                  # KRX old prune됨

    def test_contract_source_is_daily_row(self):
        """contract source = source_daily_rates의 commit된 daily row (KIS API 미호출, daily 권위)."""
        db = self.Session()
        self._seed_daily(db, contract="A99999")            # daily가 가진 contract
        self._seed_ticks(db, [(1500.0, datetime(2026, 6, 8, 0, 15))])   # KST 09:15
        KH.append_krx_cf_hourly_rows(db, date(2026, 6, 8))
        db.close()
        written = self._hourly_rows()
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].contract_code, "A99999")   # daily row contract 그대로


# ═════════════════════════════════════════════════════════════════════════════
# Layer 2 — _sync_write hook slot (gate + 격리)
# ═════════════════════════════════════════════════════════════════════════════

def _cf_close_tick() -> dict:
    """CF close grace tick (make_normalized_payload shape). date는 today (event_at_kst date)."""
    now_kst = datetime.now(KST).replace(tzinfo=None)
    dt = now_kst.replace(hour=15, minute=45, second=1, microsecond=0)
    return {
        "source": "krx", "asset": "usd-krw-futures", "session": "CF",
        "contract_code": "A75606", "price": "1490.6",
        "market_time": dt.strftime("%H%M%S"), "received_at": dt.isoformat(),
        "status": "normal",
    }


class TestSyncWriteHourlyHookSlot(unittest.IsolatedAsyncioTestCase):
    """_sync_write CF tail의 hourly hook gate + 격리. DB/Redis/flag/tether/daily 전부 mock.

    _sync_write는 staticmethod — 직접 호출. 외부 IO는 mock이라 in-memory level 검증.
    daily append는 mock(return INSERT/SKIP/HARD)으로 action 주입 → gate 분기 검증.
    """

    def setUp(self):
        from app.crawlers.krx_kis import reset_krx_close_finalizer_stats_for_tests
        reset_krx_close_finalizer_stats_for_tests()

    def _patches(self, daily_action="INSERT", daily_reason="test"):
        """DB INSERT + Redis SET + flag + tether + daily-append 모두 성공 mock 묶음."""
        return [
            patch("app.crud.insert_source_rate_unconditional", return_value=True),
            patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job", return_value=True),
            patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True),
            patch("app.latest_rates_cache.emit_krx_close_event", return_value=None),
            patch("app.tether_topic_trigger.request_tether_topic_trigger", return_value=None),
            patch("app.source_daily_rates.append_krx_cf_daily_row",
                  return_value=(daily_action, daily_reason)),
        ]

    async def _run_sync_write(self, daily_action, hourly_enabled, hourly_patch):
        """patch 묶음 적용 후 _sync_write("CF") 호출. hourly_patch = append_krx_cf_hourly_rows mock."""
        from app.crawlers.krx_kis import KrxCloseWindowWriter
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in self._patches(daily_action=daily_action):
                stack.enter_context(p)
            hourly_mock = stack.enter_context(
                patch("app.krx_hourly.append_krx_cf_hourly_rows", **hourly_patch))
            stack.enter_context(patch.object(config, "KRX_DAILY_APPEND_ENABLED", True))
            stack.enter_context(patch.object(config, "KRX_HOURLY_APPEND_ENABLED", hourly_enabled))
            result = KrxCloseWindowWriter._sync_write(_cf_close_tick(), "CF")
        return result, hourly_mock

    async def test_env_off_hourly_not_invoked(self):
        """(a) KRX_HOURLY_APPEND_ENABLED off → hourly helper 미호출 (daily INSERT여도)."""
        result, hourly_mock = await self._run_sync_write(
            daily_action="INSERT", hourly_enabled=False,
            hourly_patch={"return_value": {"inserted": 8, "updated": 0, "pruned": 0, "buckets": 8}})
        self.assertTrue(result)                            # _sync_write 정상 True
        hourly_mock.assert_not_called()                    # gate off → 미호출

    async def test_daily_insert_invokes_hourly(self):
        """(b-slot) daily INSERT + env on → hourly helper 호출 + _sync_write True."""
        result, hourly_mock = await self._run_sync_write(
            daily_action="INSERT", hourly_enabled=True,
            hourly_patch={"return_value": {"inserted": 8, "updated": 0, "pruned": 1, "buckets": 8}})
        self.assertTrue(result)
        hourly_mock.assert_called_once()
        # event_at_kst.date()가 두 번째 positional arg (date_kst)
        args, _ = hourly_mock.call_args
        self.assertEqual(args[1], datetime.now(KST).date())

    async def test_daily_skip_invokes_hourly(self):
        """(c-slot) daily SKIP + env on → hourly helper 호출 (idempotent re-roll)."""
        result, hourly_mock = await self._run_sync_write(
            daily_action="SKIP", hourly_enabled=True,
            hourly_patch={"return_value": {"inserted": 0, "updated": 8, "pruned": 0, "buckets": 8}})
        self.assertTrue(result)
        hourly_mock.assert_called_once()

    async def test_daily_hard_skips_hourly(self):
        """(d-slot) daily HARD(daily row 미생성) + env on → hourly 미호출 (contract 미resolve 차단)."""
        result, hourly_mock = await self._run_sync_write(
            daily_action="HARD", hourly_enabled=True,
            hourly_patch={"return_value": {"inserted": 0, "updated": 0, "pruned": 0, "buckets": 0}})
        self.assertTrue(result)                            # daily HARD라도 _sync_write True
        hourly_mock.assert_not_called()                    # HARD → hourly skip

    async def test_hourly_exception_isolated(self):
        """(e) hourly helper 예외 → 격리 (_sync_write 여전히 True — close finalizer 흐름 안 깨짐)."""
        result, hourly_mock = await self._run_sync_write(
            daily_action="INSERT", hourly_enabled=True,
            hourly_patch={"side_effect": RuntimeError("hourly write boom")})
        self.assertTrue(result)                            # 예외 격리 → True 유지
        hourly_mock.assert_called_once()

    async def test_daily_disabled_no_hourly_even_if_enabled(self):
        """(e-bis) daily gate off → daily_action None → hourly 미호출 (env on이어도)."""
        from app.crawlers.krx_kis import KrxCloseWindowWriter
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in self._patches(daily_action="INSERT"):
                stack.enter_context(p)
            hourly_mock = stack.enter_context(
                patch("app.krx_hourly.append_krx_cf_hourly_rows",
                      return_value={"inserted": 0, "updated": 0, "pruned": 0, "buckets": 0}))
            stack.enter_context(patch.object(config, "KRX_DAILY_APPEND_ENABLED", False))
            stack.enter_context(patch.object(config, "KRX_HOURLY_APPEND_ENABLED", True))
            result = KrxCloseWindowWriter._sync_write(_cf_close_tick(), "CF")
        self.assertTrue(result)
        hourly_mock.assert_not_called()                    # daily 미실행 → daily_action None → hourly skip


if __name__ == "__main__":
    unittest.main()
