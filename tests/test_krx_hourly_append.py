"""KRX hourly append (재설계 2026-06-10) — scripts/hourly_append_krx_source_hourly_rates.py 단위 테스트.

핵심 잠금:
  1. rollup 세션 무관 (CF/CM tick 모두 bucket 생성) + contract/session 미저장
  2. 06:00 singleton bucket 아티팩트 (CM close boundary row) — 정상
  3. plan (inserted/updated/prune/incomplete-hour 제외) — Bithumb append mirror
  4. empty-window → candidate 0 (graceful skip 전제 — KRX 고유 무세션 구간)
  5. daily-close 대조 진단 — report-only (MATCH/DIVERGE, hard gate 없음)
  6. prune은 KRX rows만 (타 source 불가침)
  7. write 통합 — contract_code None + 재설계 provenance로 영속
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import models  # noqa: E402
from app.models import SourceDailyRate, SourceHourlyRate, SourceRate  # noqa: E402
import backfill_bithumb_source_hourly_rates as B  # noqa: E402
import hourly_append_krx_source_hourly_rates as K  # noqa: E402


class TestWindow(unittest.TestCase):
    def test_previous_complete_hour(self):
        self.assertEqual(K.previous_complete_hour(datetime(2026, 6, 10, 14, 30)),
                         datetime(2026, 6, 10, 13, 0))
        self.assertEqual(K.previous_complete_hour(datetime(2026, 6, 10, 0, 5)),
                         datetime(2026, 6, 9, 23, 0))

    def test_kst_naive_to_utc_naive(self):
        # KST 14:00 = UTC 05:00
        self.assertEqual(K.kst_naive_to_utc_naive(datetime(2026, 6, 10, 14, 0)),
                         datetime(2026, 6, 10, 5, 0))


class TestRollupKrx(unittest.TestCase):
    """재설계 핵심 — 세션 무관 rollup + contract/session 미저장."""

    def test_session_agnostic_cf_and_cm(self):
        # CF 주간 tick (KST 10:30 = UTC 01:30) + CM 야간 tick (KST 23:10 = UTC 14:10)
        # + CM 새벽 tick (KST 02:10 = 전날 UTC 17:10) → 3 bucket 모두 생성
        ticks = [
            (1520.0, datetime(2026, 6, 9, 17, 10)),   # KST 6/10 02:10 → bucket 02:00
            (1521.5, datetime(2026, 6, 10, 1, 30)),   # KST 6/10 10:30 → bucket 10:00
            (1524.0, datetime(2026, 6, 10, 14, 10)),  # KST 6/10 23:10 → bucket 23:00
        ]
        rows = K.rollup_ticks_to_hourly(ticks)
        self.assertEqual(
            [r["bucket_ts_kst"] for r in rows],
            [datetime(2026, 6, 10, 2, 0), datetime(2026, 6, 10, 10, 0),
             datetime(2026, 6, 10, 23, 0)],
        )

    def test_no_contract_and_no_session_fields(self):
        # 재설계 계약: contract_code 키 자체가 없고(writer가 None 영속),
        # metadata는 point_count/first/last 3종만 (session 미포함)
        rows = K.rollup_ticks_to_hourly([(1520.0, datetime(2026, 6, 10, 1, 30))])
        r = rows[0]
        self.assertNotIn("contract_code", r)
        self.assertEqual(
            set(r["metadata_json"].keys()),
            {"point_count", "first_ts_kst", "last_ts_kst"},
        )
        self.assertEqual(r["close_basis"], "krx_observed_hourly")
        self.assertEqual(r["source_method"], "observed_rollup")
        self.assertEqual(r["ohlc_quality"], "observed_rollup")

    def test_cm_close_singleton_0600_bucket(self):
        # CM close boundary row (06:00:00 정각 단독 tick) → 06:00 bucket 싱글톤
        # (H=L=C, point_count=1) — 예상 아티팩트, 정상
        ticks = [(1519.9, datetime(2026, 6, 9, 21, 0, 0))]  # KST 6/10 06:00:00
        rows = K.rollup_ticks_to_hourly(ticks)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["bucket_ts_kst"], datetime(2026, 6, 10, 6, 0))
        self.assertEqual(r["high"], r["low"])
        self.assertEqual(r["high"], r["close"])
        self.assertEqual(r["metadata_json"]["point_count"], 1)

    def test_invariant_and_ohlc(self):
        # 같은 bucket 내 여러 tick — close=마지막, high/low 정확 + rate==close
        ticks = [
            (1520.0, datetime(2026, 6, 10, 1, 10)),
            (1525.0, datetime(2026, 6, 10, 1, 20)),
            (1518.0, datetime(2026, 6, 10, 1, 40)),
            (1522.0, datetime(2026, 6, 10, 1, 50)),
        ]
        rows = K.rollup_ticks_to_hourly(ticks)
        r = rows[0]
        self.assertEqual(r["close"], 1522.0)
        self.assertEqual(r["rate"], r["close"])
        self.assertEqual(r["high"], 1525.0)
        self.assertEqual(r["low"], 1518.0)
        self.assertEqual(r["metadata_json"]["point_count"], 4)


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()

    def _tick(self, utc_dt, rate):
        self.db.add(SourceRate(source="krx", asset="usd-krw-futures",
                               rate=rate, timestamp=utc_dt))

    def _bucket(self, kst_dt, close, source="krx", asset="usd-krw-futures",
                close_basis="krx_observed_hourly", metadata=None):
        self.db.add(SourceHourlyRate(
            source=source, asset=asset, bucket_ts_kst=kst_dt,
            rate=Decimal(str(close)), close=Decimal(str(close)),
            high=Decimal(str(close)), low=Decimal(str(close)),
            ohlc_quality="observed_rollup", close_basis=close_basis,
            source_method="observed_rollup", metadata_json=metadata))


class TestComputeAppendPlan(_DbCase):
    def test_inserted_and_prune(self):
        now = datetime(2026, 6, 10, 14, 30)  # 직전 완료 13:00
        self._tick(datetime(2026, 6, 10, 4, 30), 1521.0)   # KST 13:30 → bucket 13:00 신규
        self._bucket(datetime(2026, 5, 20, 10, 0), 1500.0)  # 14d+ old → prune 대상
        self.db.commit()

        plan = K.compute_append_plan(self.db, now, window_days=2, retention_days=14)
        self.assertEqual(plan.window_end, datetime(2026, 6, 10, 13, 0))
        self.assertEqual(plan.candidate_count, 1)
        self.assertEqual(plan.inserted, 1)
        self.assertEqual(plan.prune_count, 1)

    def test_current_incomplete_hour_excluded(self):
        now = datetime(2026, 6, 10, 14, 30)
        self._tick(datetime(2026, 6, 10, 5, 30), 1523.0)   # KST 14:30 (현재 hour) — 제외
        self._tick(datetime(2026, 6, 10, 4, 30), 1521.0)   # KST 13:30 (직전 완료)
        self.db.commit()

        plan = K.compute_append_plan(self.db, now, window_days=2, retention_days=14)
        self.assertEqual(plan.candidate_count, 1)
        self.assertEqual(plan.latest_bucket_ts, datetime(2026, 6, 10, 13, 0))

    def test_empty_window_zero_candidates(self):
        # KRX 고유: 주말~월요일 아침 등 무세션 구간 — candidate 0 (graceful skip 전제)
        now = datetime(2026, 6, 8, 7, 30)  # 월요일 아침 (토 06:00 후 무거래)
        self.db.commit()

        plan = K.compute_append_plan(self.db, now, window_days=2, retention_days=14)
        self.assertEqual(plan.candidate_count, 0)
        self.assertIsNone(plan.latest_bucket_ts)
        self.assertEqual(plan.daily_close_reports, [])

    def test_updated_same_with_identical_metadata(self):
        now = datetime(2026, 6, 10, 14, 30)
        self._tick(datetime(2026, 6, 10, 4, 30), 1521.0)   # KST 13:30 → bucket 13:00
        cand = K.rollup_ticks_to_hourly([(1521.0, datetime(2026, 6, 10, 4, 30))])[0]
        self._bucket(datetime(2026, 6, 10, 13, 0), 1521.0, metadata=cand["metadata_json"])
        self.db.commit()

        plan = K.compute_append_plan(self.db, now, window_days=2, retention_days=14)
        self.assertEqual(plan.inserted, 0)
        self.assertEqual(plan.updated_same, 1)


class TestDailyCloseDiagnostic(_DbCase):
    def _daily(self, d, close, contract="A75606"):
        self.db.add(SourceDailyRate(
            source="krx", asset="usd-krw-futures", date_kst=d,
            rate=Decimal(str(close)), close=Decimal(str(close)),
            high=Decimal(str(close)), low=Decimal(str(close)),
            ohlc_quality="observed_rollup", close_basis="krx_cf_close_1545",
            source_method="close_finalizer", contract_code=contract))

    def test_match_and_diverge_report_only(self):
        # 6/10: 15:00 bucket close == daily close → MATCH
        # 6/9: 15:00 bucket close(blackout 관측치) != daily close(openapi 복구) → DIVERGE
        self._daily(date(2026, 6, 10), 1524.6)
        self._daily(date(2026, 6, 9), 1514.7)
        self.db.commit()
        candidates = [
            K.rollup_ticks_to_hourly([(1524.6, datetime(2026, 6, 10, 6, 45))])[0],  # KST 6/10 15:45
            K.rollup_ticks_to_hourly([(1510.6, datetime(2026, 6, 9, 6, 4))])[0],    # KST 6/9 15:04
        ]
        reports = K.daily_close_diagnostic(self.db, candidates)
        self.assertEqual(len(reports), 2)
        joined = "\n".join(reports)
        self.assertIn("2026-06-10", joined)
        self.assertIn("MATCH", joined)
        self.assertIn("2026-06-09", joined)
        self.assertIn("DIVERGE", joined)

    def test_non_1500_bucket_and_missing_daily_ignored(self):
        self._daily(date(2026, 6, 10), 1524.6)
        self.db.commit()
        candidates = [
            # 14:00 bucket — 진단 대상 아님
            K.rollup_ticks_to_hourly([(1523.0, datetime(2026, 6, 10, 5, 30))])[0],
            # 6/11 15:00 bucket — daily row 없음 → 제외
            K.rollup_ticks_to_hourly([(1525.0, datetime(2026, 6, 11, 6, 30))])[0],
        ]
        reports = K.daily_close_diagnostic(self.db, candidates)
        self.assertEqual(reports, [])


class TestPruneKrxOnly(_DbCase):
    def test_prune_does_not_touch_other_sources(self):
        old = datetime(2026, 5, 20, 10, 0)
        self._bucket(old, 1500.0)  # krx — prune 대상
        self._bucket(old, 1400.0, source="bithumb", asset="usdt-krw",
                     close_basis="bithumb_observed_hourly")  # 타 source — 불가침
        self.db.commit()

        deleted = K.prune_old_buckets(self.db, datetime(2026, 5, 27, 0, 0))
        self.assertEqual(deleted, 1)
        remaining = self.db.query(SourceHourlyRate).all()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].source, "bithumb")


class TestWriteIntegration(_DbCase):
    """B.write_with_transaction(KRX params, default allow_contract_code=False) 통합 —
    contract_code None + 재설계 provenance 영속."""

    def test_write_persists_contract_none(self):
        self._tick(datetime(2026, 6, 10, 4, 30), 1521.0)   # KST 13:30
        self._tick(datetime(2026, 6, 9, 21, 0), 1519.9)    # KST 6/10 06:00 (CM close singleton)
        self.db.commit()
        _, _, candidates = K._fetch_candidates(
            self.db, datetime(2026, 6, 10, 14, 30), window_days=2)
        self.assertEqual(len(candidates), 2)

        with patch("app.database.SessionLocal", self.Session):
            success, issues, outcome = B.write_with_transaction(
                candidates, require_empty=False,
                source=K.SOURCE, asset=K.ASSET, close_basis=K.CLOSE_BASIS,
                source_method=K.SOURCE_METHOD, ohlc_quality=K.OHLC_QUALITY,
            )
        self.assertTrue(success, f"issues: {issues}")
        self.assertEqual(outcome["inserted"], 2)

        written = (self.db.query(SourceHourlyRate)
                   .filter(SourceHourlyRate.source == "krx").all())
        self.assertEqual(len(written), 2)
        for w in written:
            self.assertIsNone(w.contract_code)          # 재설계 계약
            self.assertEqual(w.close_basis, "krx_observed_hourly")
            self.assertEqual(w.rate, w.close)
            self.assertNotIn("session", w.metadata_json or {})

    def test_write_idempotent_rerun(self):
        self._tick(datetime(2026, 6, 10, 4, 30), 1521.0)
        self.db.commit()
        _, _, candidates = K._fetch_candidates(
            self.db, datetime(2026, 6, 10, 14, 30), window_days=2)

        with patch("app.database.SessionLocal", self.Session):
            s1, _, o1 = B.write_with_transaction(
                candidates, require_empty=False,
                source=K.SOURCE, asset=K.ASSET, close_basis=K.CLOSE_BASIS,
                source_method=K.SOURCE_METHOD, ohlc_quality=K.OHLC_QUALITY)
            s2, _, o2 = B.write_with_transaction(
                candidates, require_empty=False,
                source=K.SOURCE, asset=K.ASSET, close_basis=K.CLOSE_BASIS,
                source_method=K.SOURCE_METHOD, ohlc_quality=K.OHLC_QUALITY)
        self.assertTrue(s1 and s2)
        self.assertEqual(o1["inserted"], 1)
        self.assertEqual(o2["inserted"], 0)
        self.assertEqual(o2["updated"], 1)
        count = (self.db.query(SourceHourlyRate)
                 .filter(SourceHourlyRate.source == "krx").count())
        self.assertEqual(count, 1)

    def test_trip_wire_old_contract_row_fails_closed(self):
        """④ 재구축 'delete 깜빡' 안전망 영구 회귀 — 구 contract 보유 row 위에 신
        write(allow_contract_code=False) 시 H.upsert null-보존(contract 유지) ×
        post-write 검증(d, 'contract NOT None → issue') → fail-close + rollback +
        구 row 원형 보존. 2026-06-10 ⑥에서 삭제된 test_krx_source_hourly_rates.py의
        True-arm 커버 대신, 더 중요한 False-arm rejection(모든 caller live 경로)을 잠근다.
        """
        from decimal import Decimal
        # 구 hook 스타일 row (contract=A75606 + session metadata) — delete 누락 시뮬
        self.db.add(SourceHourlyRate(
            source="krx", asset="usd-krw-futures",
            bucket_ts_kst=datetime(2026, 6, 10, 13, 0),
            rate=Decimal("1520.0"), close=Decimal("1520.0"),
            high=Decimal("1522.0"), low=Decimal("1519.0"),
            ohlc_quality="observed_rollup", close_basis="krx_observed_hourly",
            source_method="observed_rollup", contract_code="A75606",
            metadata_json={"session": "CF", "point_count": 99}))
        self.db.commit()
        # 신 candidate (같은 bucket, contract None)
        self._tick(datetime(2026, 6, 10, 4, 30), 1521.0)   # KST 13:30 → bucket 13:00
        self.db.commit()
        _, _, candidates = K._fetch_candidates(
            self.db, datetime(2026, 6, 10, 14, 30), window_days=2)

        with patch("app.database.SessionLocal", self.Session):
            success, issues, outcome = B.write_with_transaction(
                candidates, require_empty=False,
                source=K.SOURCE, asset=K.ASSET, close_basis=K.CLOSE_BASIS,
                source_method=K.SOURCE_METHOD, ohlc_quality=K.OHLC_QUALITY,
            )
        self.assertFalse(success)
        self.assertTrue(any("contract_code NOT None" in i for i in issues),
                        f"issues: {issues}")
        # rollback — 구 row 원형 보존 (contract/close/metadata 불변)
        self.db.expire_all()
        r = self.db.query(SourceHourlyRate).filter(SourceHourlyRate.source == "krx").one()
        self.assertEqual(r.contract_code, "A75606")
        self.assertEqual(float(r.close), 1520.0)
        self.assertEqual(r.metadata_json, {"session": "CF", "point_count": 99})


if __name__ == "__main__":
    unittest.main(verbosity=2)
