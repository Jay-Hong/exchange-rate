"""KRX CF append write guard (Unit 3) — decide_krx_cf_append_action + write_krx_cf_append_row 테스트.

decide: pure (DB 불필요). write: in-memory SQLite.
  decide: existing None INSERT / incoming source·asset·method 방어 HARD / key mismatch HARD /
          예상 밖 existing method HARD / pair1·2 close match SKIP / high·low diff SKIP+reason / close mismatch HARD
  write: 신규 INSERT(db.add) / 기존 동일 close SKIP(기존 불변) / mismatch HARD(write 0)
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.models import SourceDailyRate  # noqa: E402
from app.source_daily_rates import (  # noqa: E402
    CfSessionRollup,
    KRX_APPEND_HARD,
    KRX_APPEND_INSERT,
    KRX_APPEND_SKIP,
    build_krx_cf_append_row,
    decide_krx_cf_append_action,
    write_krx_cf_append_row,
)

_D = date(2026, 6, 3)


def _incoming(close=1505.0, high=1510.0, low=1495.0, point_count=10, contract="A75606"):
    """valid incoming row (build_krx_cf_append_row 경유)."""
    rollup = CfSessionRollup(high=high, low=low, point_count=point_count, first_ts=None, last_ts=None)
    return build_krx_cf_append_row(_D, close, rollup, contract)


def _existing(*, source_method="krx_openapi_daily", close="1505.0", high="1510.0", low="1495.0",
              source="krx", asset="usd-krw-futures", date_kst=_D, contract="A75606"):
    return SourceDailyRate(
        source=source, asset=asset, date_kst=date_kst,
        rate=Decimal(close), high=Decimal(high), low=Decimal(low), close=Decimal(close),
        ohlc_quality="source_ohlc", close_basis="krx_cf_close_1545",
        source_method=source_method, contract_code=contract,
        basis_date=None, published_at=None, metadata_json={},
    )


class TestDecideKrxCfAppendAction(unittest.TestCase):
    def test_existing_none_insert(self):
        action, _ = decide_krx_cf_append_action(None, _incoming())
        self.assertEqual(action, KRX_APPEND_INSERT)

    def test_incoming_wrong_asset_hard(self):
        row = _incoming()
        row["asset"] = "usdt-krw"
        action, reason = decide_krx_cf_append_action(None, row)
        self.assertEqual(action, KRX_APPEND_HARD)
        self.assertIn("source/asset", reason)

    def test_incoming_wrong_source_hard(self):
        # KRX 전용 guard — incoming source != krx면 HARD (existing None이어도 insert 안 함)
        row = _incoming()
        row["source"] = "bithumb"
        action, reason = decide_krx_cf_append_action(None, row)
        self.assertEqual(action, KRX_APPEND_HARD)
        self.assertIn("source/asset", reason)

    def test_incoming_wrong_method_hard(self):
        row = _incoming()
        row["source_method"] = "krx_openapi_daily"  # close_finalizer 아님
        action, reason = decide_krx_cf_append_action(None, row)
        self.assertEqual(action, KRX_APPEND_HARD)
        self.assertIn("source_method", reason)

    def test_key_mismatch_hard(self):
        ex = _existing(date_kst=date(2026, 6, 2))  # incoming date(6/3)과 다름
        action, reason = decide_krx_cf_append_action(ex, _incoming())
        self.assertEqual(action, KRX_APPEND_HARD)
        self.assertIn("key mismatch", reason)

    def test_existing_source_asset_mismatch_hard(self):
        # pure 함수 방어 — existing source/asset이 incoming과 다르면 HARD (caller 조회 실수 fail-loud)
        for ex in (_existing(source="bithumb"), _existing(asset="usdt-krw")):
            action, reason = decide_krx_cf_append_action(ex, _incoming())
            self.assertEqual(action, KRX_APPEND_HARD)
            self.assertIn("key mismatch", reason)

    def test_unexpected_existing_method_hard(self):
        ex = _existing(source_method="hana_observed_eod")
        action, reason = decide_krx_cf_append_action(ex, _incoming())
        self.assertEqual(action, KRX_APPEND_HARD)
        self.assertIn("provenance", reason)

    def test_pair1_close_match_skip(self):
        ex = _existing(source_method="krx_openapi_daily", close="1505.0", high="1510.0", low="1495.0")
        action, _ = decide_krx_cf_append_action(ex, _incoming(close=1505.0, high=1510.0, low=1495.0))
        self.assertEqual(action, KRX_APPEND_SKIP)

    def test_pair1_high_low_differ_skip_reason(self):
        ex = _existing(source_method="krx_openapi_daily", close="1505.0", high="1512.0", low="1490.0")
        action, reason = decide_krx_cf_append_action(ex, _incoming(close=1505.0, high=1510.0, low=1495.0))
        self.assertEqual(action, KRX_APPEND_SKIP)
        self.assertIn("high/low differ", reason)

    def test_pair2_close_match_skip(self):
        ex = _existing(source_method="close_finalizer", close="1505.0", high="1510.0", low="1495.0")
        action, _ = decide_krx_cf_append_action(ex, _incoming(close=1505.0, high=1510.0, low=1495.0))
        self.assertEqual(action, KRX_APPEND_SKIP)

    def test_close_mismatch_hard(self):
        ex = _existing(source_method="krx_openapi_daily", close="1500.0")
        action, reason = decide_krx_cf_append_action(ex, _incoming(close=1505.0))
        self.assertEqual(action, KRX_APPEND_HARD)
        self.assertIn("close mismatch", reason)


class TestWriteKrxCfAppendRow(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _count(self, db):
        return db.query(func.count()).select_from(SourceDailyRate).scalar()

    def test_new_date_insert(self):
        with self.Session() as db:
            action, _ = write_krx_cf_append_row(db, _incoming(close=1505.0))
            self.assertEqual(action, KRX_APPEND_INSERT)
            db.commit()
        with self.Session() as db:
            self.assertEqual(self._count(db), 1)
            r = db.query(SourceDailyRate).one()
            self.assertEqual(r.source_method, "close_finalizer")
            self.assertEqual(r.close, Decimal("1505.0"))

    def test_existing_same_close_skip_unchanged(self):
        with self.Session() as db:
            db.add(_existing(source_method="krx_openapi_daily", close="1505.0"))
            db.commit()
            orig_id = db.query(SourceDailyRate).one().id
        with self.Session() as db:
            action, _ = write_krx_cf_append_row(db, _incoming(close=1505.0))
            self.assertEqual(action, KRX_APPEND_SKIP)
            db.commit()
        with self.Session() as db:
            self.assertEqual(self._count(db), 1)  # 신규 row 없음
            r = db.query(SourceDailyRate).one()
            self.assertEqual(r.id, orig_id)                          # 기존 row 그대로
            self.assertEqual(r.source_method, "krx_openapi_daily")   # 불변 (덮지 않음)

    def test_close_mismatch_hard_no_write(self):
        with self.Session() as db:
            db.add(_existing(source_method="krx_openapi_daily", close="1500.0"))
            db.commit()
        with self.Session() as db:
            action, _ = write_krx_cf_append_row(db, _incoming(close=1505.0))
            self.assertEqual(action, KRX_APPEND_HARD)
            db.rollback()
        with self.Session() as db:
            self.assertEqual(self._count(db), 1)  # write 0
            self.assertEqual(db.query(SourceDailyRate).one().close, Decimal("1500.0"))


if __name__ == "__main__":
    unittest.main()
