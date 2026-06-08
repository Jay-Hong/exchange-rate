"""KRX hourly CF-only dry-run validator 단위 테스트 (ADR-035 D3 Step 2).

scripts/backfill_krx_source_hourly_rates.py의 CF-session filter rollup + contract resolver +
session/contract/enum validation을 synthetic으로 검증. write 0. 외부 의존성 0.

KST = UTC + 9 (source_rates.timestamp는 UTC naive). 예: KST 09:15 = UTC 00:15 / KST 08:30 = UTC 전날 23:30.

검증 포인트:
- rollup_cf_ticks_to_hourly: CF 세션(08:30~15:45 KST) tick만 → CM 야간 drop / OHLC / session=CF / contract resolve
- CF 경계 inclusive (08:30:00 / 15:45:00) + 15:46 drop
- validate_session: CM 누출(hour ∉ [8,15]) + 잘못된 label 검출
- validate_contract: contract None(daily 부재) 검출
- validate_enum: krx_observed_hourly 외(daily enum 등) 차단
- fetch_daily_contract_map: source_daily_rates source/asset/range 필터
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import models  # noqa: E402
from app.models import SourceDailyRate, SourceHourlyRate  # noqa: E402
import backfill_bithumb_source_hourly_rates as B  # noqa: E402
import backfill_krx_source_hourly_rates as K  # noqa: E402


class TestRollupCf(unittest.TestCase):
    def test_cf_filter_drops_cm_and_rolls_up(self):
        cmap = {date(2026, 6, 8): "A75606"}
        ticks = [
            (1500.0, datetime(2026, 6, 8, 0, 15)),   # KST 09:15 CF
            (1502.0, datetime(2026, 6, 8, 0, 30)),   # KST 09:30 CF (high)
            (1499.0, datetime(2026, 6, 8, 0, 45)),   # KST 09:45 CF (close, low)
            (1490.0, datetime(2026, 6, 8, 10, 0)),   # KST 19:00 CM 야간 → drop
        ]
        rows = K.rollup_cf_ticks_to_hourly(ticks, cmap)
        self.assertEqual(len(rows), 1)                            # CF 09:00 only (CM drop)
        r = rows[0]
        self.assertEqual(r["bucket_ts_kst"], datetime(2026, 6, 8, 9, 0))
        self.assertEqual(r["close"], 1499.0)                      # 마지막 CF tick
        self.assertEqual(r["rate"], r["close"])                   # invariant
        self.assertEqual(r["high"], 1502.0)                       # CM 1490 미반영
        self.assertEqual(r["low"], 1499.0)
        self.assertEqual(r["close_basis"], "krx_observed_hourly")
        self.assertEqual(r["source_method"], "observed_rollup")
        self.assertEqual(r["contract_code"], "A75606")
        self.assertEqual(r["metadata_json"]["session"], "CF")
        self.assertEqual(r["metadata_json"]["point_count"], 3)    # CF 3개 (CM 제외)

    def test_cf_boundary_inclusive(self):
        # KST 08:30:00(=UTC 전날 23:30) / 15:45:00(=UTC 06:45) 경계 inclusive, 15:46 drop
        cmap = {date(2026, 6, 8): "A75606"}
        ticks = [
            (1500.0, datetime(2026, 6, 7, 23, 30)),  # KST 08:30:00 → 08:00 bucket
            (1501.0, datetime(2026, 6, 8, 6, 45)),   # KST 15:45:00 → 15:00 bucket
            (1502.0, datetime(2026, 6, 8, 6, 46)),   # KST 15:46:00 → CF 밖 drop
        ]
        rows = K.rollup_cf_ticks_to_hourly(ticks, cmap)
        self.assertEqual(sorted(r["bucket_ts_kst"].hour for r in rows), [8, 15])  # 경계 inclusive

    def test_contract_missing_none(self):
        ticks = [(1500.0, datetime(2026, 6, 8, 0, 15))]           # KST 09:15 CF
        rows = K.rollup_cf_ticks_to_hourly(ticks, {})             # daily row 부재
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["contract_code"])               # 부재 → None

    def test_empty(self):
        self.assertEqual(K.rollup_cf_ticks_to_hourly([], {}), [])


class TestValidations(unittest.TestCase):
    def _row(self, hour=9, session="CF", contract="A75606", close_basis="krx_observed_hourly"):
        return {
            "source": "krx", "asset": "usd-krw-futures",
            "bucket_ts_kst": datetime(2026, 6, 8, hour, 0),
            "rate": 1500.0, "close": 1500.0, "high": 1500.0, "low": 1500.0,
            "ohlc_quality": "observed_rollup", "close_basis": close_basis,
            "source_method": "observed_rollup", "contract_code": contract,
            "metadata_json": {"point_count": 1, "first_ts_kst": "x", "last_ts_kst": "x", "session": session},
        }

    def test_session_catches_cm_leak(self):
        issues = K.validate_session([self._row(hour=19)])         # CM 야간 hour
        self.assertTrue(any("[8,15]" in i for i in issues))

    def test_session_catches_wrong_label(self):
        issues = K.validate_session([self._row(session="CM")])
        self.assertTrue(any("session != CF" in i for i in issues))

    def test_session_ok(self):
        self.assertEqual(K.validate_session([self._row()]), [])

    def test_contract_flags_missing(self):
        issues = K.validate_contract([self._row(contract=None)])
        self.assertTrue(any("contract_code 미resolve" in i for i in issues))

    def test_contract_dedup_dates(self):
        # 같은 date 여러 bucket이 None이어도 issue는 date당 1회
        rows = [self._row(hour=9, contract=None), self._row(hour=10, contract=None)]
        self.assertEqual(len(K.validate_contract(rows)), 1)       # 2026-06-08 1회

    def test_enum_catches_daily_close_basis(self):
        issues = K.validate_enum([self._row(close_basis="krx_cf_close_1545")])  # daily enum → hourly 불가
        self.assertTrue(any("close_basis 위반" in i for i in issues))

    def test_enum_ok(self):
        self.assertEqual(K.validate_enum([self._row()]), [])


class TestFetchDailyContractMap(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def test_lookup_filters_source_asset_range(self):
        self.db.add_all([
            SourceDailyRate(source="krx", asset="usd-krw-futures", date_kst=date(2026, 6, 8),
                            rate=1500, close=1500, ohlc_quality="source_ohlc",
                            close_basis="krx_cf_close_1545", source_method="krx_openapi_daily",
                            contract_code="A75606"),
            SourceDailyRate(source="krx", asset="usd-krw-futures", date_kst=date(2026, 5, 1),   # 범위 밖
                            rate=1490, close=1490, ohlc_quality="source_ohlc",
                            close_basis="krx_cf_close_1545", source_method="krx_openapi_daily",
                            contract_code="A75605"),
            SourceDailyRate(source="bithumb", asset="usdt-krw", date_kst=date(2026, 6, 8),       # 다른 source
                            rate=1400, close=1400, ohlc_quality="source_ohlc",
                            close_basis="bithumb_24h_kst_close", source_method="bithumb_candlestick_api"),
        ])
        self.db.commit()
        cmap = K.fetch_daily_contract_map(self.db, date(2026, 6, 7), date(2026, 6, 9))
        self.assertEqual(cmap, {date(2026, 6, 8): "A75606"})      # krx + 범위 안만


class TestWritePath(unittest.TestCase):
    """Step 3a write path — B.write_with_transaction 확장 재사용 (allow_contract_code=True +
    krx_post_write_validator). in-memory SQLite + app.database.SessionLocal patch (Hana/Bithumb 패턴).
    """

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def _cf_rows(self, contract="A75606"):
        """2026-06-08 CF buckets 2개 (09:00 + 15:00, 15:45 inclusive). contract=None이면 미resolve."""
        cmap = {date(2026, 6, 8): contract} if contract is not None else {}
        ticks = [
            (1500.0, datetime(2026, 6, 8, 0, 15)),   # KST 09:15 CF
            (1502.0, datetime(2026, 6, 8, 0, 30)),   # KST 09:30 high
            (1499.0, datetime(2026, 6, 8, 0, 45)),   # KST 09:45 close(09)
            (1505.0, datetime(2026, 6, 8, 6, 44)),   # KST 15:44
            (1506.0, datetime(2026, 6, 8, 6, 45)),   # KST 15:45 close(15) inclusive
            (1490.0, datetime(2026, 6, 8, 10, 0)),   # KST 19:00 CM → drop
        ]
        return K.rollup_cf_ticks_to_hourly(ticks, cmap)

    def _written(self):
        db = self.Session()
        try:
            return (db.query(SourceHourlyRate)
                    .filter(SourceHourlyRate.source == "krx")
                    .order_by(SourceHourlyRate.bucket_ts_kst).all())
        finally:
            db.close()

    def test_write_persists_contract_and_session(self):
        """(a) write 성공 → contract_code(A75606) + session=CF persisted, CM drop, rate==close."""
        rows = self._cf_rows(contract="A75606")
        self.assertEqual(len(rows), 2)   # CF 09:00 + 15:00 (CM drop)
        with patch("app.database.SessionLocal", self.Session):
            success, issues, outcome = B.write_with_transaction(
                rows, require_empty=True,
                source="krx", asset="usd-krw-futures", close_basis="krx_observed_hourly",
                source_method="observed_rollup", ohlc_quality="observed_rollup",
                allow_contract_code=True, post_write_validator=K.krx_post_write_validator)
        self.assertTrue(success, issues)
        self.assertEqual(outcome, {"inserted": 2, "updated": 0})
        written = self._written()
        self.assertEqual([w.bucket_ts_kst.hour for w in written], [9, 15])         # CM dropped
        self.assertTrue(all(w.contract_code == "A75606" for w in written))         # contract 보존
        self.assertTrue(all((w.metadata_json or {}).get("session") == "CF" for w in written))
        self.assertTrue(all(w.rate == w.close for w in written))                   # invariant
        # basis_date/published_at는 hourly observed라 None
        self.assertTrue(all(w.basis_date is None and w.published_at is None for w in written))
        b15 = [w for w in written if w.bucket_ts_kst.hour == 15][0]
        self.assertEqual(float(b15.close), 1506.0)                                 # 15:45 inclusive

    def test_require_empty_rejects_nonempty_target(self):
        """(b) --require-empty-target → 기존 row 있으면 reject (gap-only 위반)."""
        rows = self._cf_rows(contract="A75606")
        with patch("app.database.SessionLocal", self.Session):
            ok1, _, _ = B.write_with_transaction(
                rows, require_empty=True,
                source="krx", asset="usd-krw-futures", close_basis="krx_observed_hourly",
                source_method="observed_rollup", ohlc_quality="observed_rollup",
                allow_contract_code=True, post_write_validator=K.krx_post_write_validator)
            self.assertTrue(ok1)   # 1차 적재
            # 동일 target에 require_empty=True 재시도 → reject
            ok2, issues2, _ = B.write_with_transaction(
                rows, require_empty=True,
                source="krx", asset="usd-krw-futures", close_basis="krx_observed_hourly",
                source_method="observed_rollup", ohlc_quality="observed_rollup",
                allow_contract_code=True, post_write_validator=K.krx_post_write_validator)
        self.assertFalse(ok2)
        self.assertTrue(any("require-empty-target" in i for i in issues2))
        self.assertEqual(len(self._written()), 2)   # 1차 2 row 그대로 (2차는 미반영)

    def test_contract_none_post_write_rollback(self):
        """(c) contract None(daily 부재) → in-transaction post-write 검증 실패 → rollback (0 row)."""
        rows = self._cf_rows(contract=None)   # contract_map 부재 → 모든 row contract None
        self.assertTrue(all(r["contract_code"] is None for r in rows))
        with patch("app.database.SessionLocal", self.Session):
            success, issues, outcome = B.write_with_transaction(
                rows, require_empty=True,
                source="krx", asset="usd-krw-futures", close_basis="krx_observed_hourly",
                source_method="observed_rollup", ohlc_quality="observed_rollup",
                allow_contract_code=True, post_write_validator=K.krx_post_write_validator)
        self.assertFalse(success)
        self.assertTrue(any("contract_code IS NULL" in i for i in issues))
        self.assertEqual(outcome, {"inserted": 0, "updated": 0})
        self.assertEqual(len(self._written()), 0)   # rollback — 0 row

    def test_session_not_cf_post_write_rollback(self):
        """(c-bis) metadata session != CF (CM 누출 등) → post-write 검증 실패 → rollback (0 row)."""
        rows = self._cf_rows(contract="A75606")
        rows[0]["metadata_json"]["session"] = "CM"   # 강제 변조 (CM 누출 시뮬)
        with patch("app.database.SessionLocal", self.Session):
            success, issues, _ = B.write_with_transaction(
                rows, require_empty=True,
                source="krx", asset="usd-krw-futures", close_basis="krx_observed_hourly",
                source_method="observed_rollup", ohlc_quality="observed_rollup",
                allow_contract_code=True, post_write_validator=K.krx_post_write_validator)
        self.assertFalse(success)
        self.assertTrue(any("session != CF" in i for i in issues))
        self.assertEqual(len(self._written()), 0)   # rollback — 0 row


class TestWriteBackwardCompat(unittest.TestCase):
    """B 확장(allow_contract_code / post_write_validator)이 generic source(Bithumb/Investing/Hana)에
    영향 0임을 KRX 테스트 파일에서도 직접 확인 (default 미전달 = contract None 강제 + validator 미호출).
    """

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_default_path_unchanged_contract_none(self):
        """default(allow_contract_code 미전달) → contract_code 무시(None) + validator 미호출 → 정상 적재.

        candidate에 contract_code가 있어도 default path는 upsert에 미전달 → None persist (generic 계약).
        """
        rows = B.rollup_ticks_to_hourly([
            (1400.0, datetime(2026, 6, 8, 5, 0)),    # KST 14:00
            (1401.0, datetime(2026, 6, 8, 5, 30)),
        ])
        rows[0]["contract_code"] = "SHOULD_BE_IGNORED"   # default path는 무시해야 함
        with patch("app.database.SessionLocal", self.Session):
            success, issues, outcome = B.write_with_transaction(rows, require_empty=True)
        self.assertTrue(success, issues)
        self.assertEqual(outcome, {"inserted": 1, "updated": 0})
        db = self.Session()
        try:
            w = db.query(SourceHourlyRate).filter(SourceHourlyRate.source == "bithumb").one()
        finally:
            db.close()
        self.assertIsNone(w.contract_code)   # default path → contract_code None (generic 불변)


if __name__ == "__main__":
    unittest.main()
