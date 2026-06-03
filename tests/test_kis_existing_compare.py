"""Step 4B 단위 3 — manifest × 기존 KRX seed 비교 (4-way 분류) 단위 테스트.

2층:
  1) compare_manifest_with_existing 순수 로직 (dict 입력, 4-way + 비교 키 정책)
  2) query_existing_krx_rows + compare 통합 (실제 SQLAlchemy in-memory round-trip —
     Numeric(14,6)→float / JSON / null 동작까지 검증, production DB 무관)
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_kis_source_daily_rates as B  # noqa: E402

_WS = date(2026, 6, 1)
_WE = date(2026, 6, 12)


def _manifest_row(d: date, close: str = "1496.5", contract_code: str = "A75606", **over) -> dict:
    """parse_daily_rows 형태 (Decimal top-level, metadata_json dict)."""
    row = {
        "source": "krx", "asset": "usd-krw-futures", "date_kst": d,
        "rate": Decimal(close), "high": Decimal(close), "low": Decimal(close), "close": Decimal(close),
        "ohlc_quality": "source_ohlc", "close_basis": "krx_cf_close_1545",
        "source_method": "kis_daily_backfill", "contract_code": contract_code,
        "basis_date": None, "published_at": None,
        "metadata_json": {
            "contract_short_code": contract_code, "contract_month": "202606",
            "contract_expiry_date": "2026-06-15", "open": close,
        },
    }
    row.update(over)
    return row


def _existing_dict(d: date, close: float = 1496.5, contract_code: str = "A75606", **over) -> dict:
    """row_to_dict 형태 (Numeric→float)."""
    row = {
        "source": "krx", "asset": "usd-krw-futures", "date_kst": d,
        "rate": close, "high": close, "low": close, "close": close,
        "ohlc_quality": "source_ohlc", "close_basis": "krx_cf_close_1545",
        "source_method": "kis_daily_backfill", "contract_code": contract_code,
        "basis_date": None, "published_at": None,
        "metadata_json": {
            "contract_short_code": contract_code, "contract_month": "202606",
            "contract_expiry_date": "2026-06-15", "open": str(close),
        },
    }
    row.update(over)
    return row


class TestCompareFourWay(unittest.TestCase):
    def test_manifest_only_to_write(self):
        res = B.compare_manifest_with_existing([_manifest_row(date(2026, 6, 10))], [], _WS, _WE)
        self.assertEqual(len(res.rows_to_write), 1)
        self.assertEqual(res.hard_issues, [])
        self.assertEqual(res.matched_existing, [])

    def test_matched_skip(self):
        d = date(2026, 6, 10)
        res = B.compare_manifest_with_existing([_manifest_row(d)], [_existing_dict(d)], _WS, _WE)
        self.assertEqual(res.rows_to_write, [])
        self.assertEqual(len(res.matched_existing), 1)
        self.assertEqual(res.hard_issues, [])

    def test_decimal_roundtrip_match(self):
        # 핵심: manifest Decimal("1496.5") vs existing float 1496.5 → 일치 (1496.5 == 1496.500000)
        d = date(2026, 6, 10)
        res = B.compare_manifest_with_existing(
            [_manifest_row(d, close="1496.5")], [_existing_dict(d, close=1496.5)], _WS, _WE
        )
        self.assertEqual(res.hard_issues, [])
        self.assertEqual(len(res.matched_existing), 1)

    def test_close_numeric_conflict_hard(self):
        d = date(2026, 6, 10)
        res = B.compare_manifest_with_existing(
            [_manifest_row(d, close="1496.5")], [_existing_dict(d, close=1496.6)], _WS, _WE
        )
        self.assertTrue(any("close numeric mismatch" in i for i in res.hard_issues))

    def test_literal_conflict_hard(self):
        d = date(2026, 6, 10)
        m = _manifest_row(d, contract_code="A75606")
        e = _existing_dict(d, contract_code="A75607")  # contract_code 다름
        res = B.compare_manifest_with_existing([m], [e], _WS, _WE)
        self.assertTrue(any("contract_code mismatch" in i for i in res.hard_issues))

    def test_null_violation_hard(self):
        d = date(2026, 6, 10)
        m = _manifest_row(d)
        e = _existing_dict(d, basis_date=date(2026, 6, 1))  # KRX인데 basis_date not None
        res = B.compare_manifest_with_existing([m], [e], _WS, _WE)
        self.assertTrue(any("basis_date must be None" in i for i in res.hard_issues))

    def test_metadata_identity_missing_hard(self):
        d = date(2026, 6, 10)
        e = _existing_dict(d)
        del e["metadata_json"]["contract_short_code"]  # identity 누락
        res = B.compare_manifest_with_existing([_manifest_row(d)], [e], _WS, _WE)
        self.assertTrue(any("metadata.contract_short_code 누락" in i for i in res.hard_issues))

    def test_metadata_identity_mismatch_hard(self):
        d = date(2026, 6, 10)
        e = _existing_dict(d)
        e["metadata_json"]["contract_month"] = "202607"  # identity 불일치
        res = B.compare_manifest_with_existing([_manifest_row(d)], [e], _WS, _WE)
        self.assertTrue(any("metadata.contract_month mismatch" in i for i in res.hard_issues))

    def test_open_missing_surface_not_hard(self):
        d = date(2026, 6, 10)
        e = _existing_dict(d)
        del e["metadata_json"]["open"]  # open 누락 → surface (hard 아님)
        res = B.compare_manifest_with_existing([_manifest_row(d)], [e], _WS, _WE)
        self.assertEqual(res.hard_issues, [])
        self.assertTrue(any("open 누락" in w for w in res.warnings))

    def test_open_mismatch_surface_not_hard(self):
        d = date(2026, 6, 10)
        e = _existing_dict(d)
        e["metadata_json"]["open"] = "1499.9"  # open 다름 → surface
        res = B.compare_manifest_with_existing([_manifest_row(d)], [e], _WS, _WE)
        self.assertEqual(res.hard_issues, [])
        self.assertTrue(any("open mismatch" in w for w in res.warnings))

    def test_orphan_existing_within_window_hard(self):
        # ④ DB(window 안)엔 있는데 manifest에 없음 → hard
        d = date(2026, 6, 10)
        res = B.compare_manifest_with_existing([], [_existing_dict(d)], _WS, _WE)
        self.assertTrue(any("orphan" in i for i in res.hard_issues))

    def test_existing_outside_window_hard(self):
        # query 제한 위반 방어 (defensive): existing이 window 밖
        d = date(2026, 1, 1)  # window [2026-06-01, 2026-06-12] 밖
        res = B.compare_manifest_with_existing([], [_existing_dict(d)], _WS, _WE)
        self.assertTrue(any("window" in i and "밖" in i for i in res.hard_issues))

    def test_manifest_duplicate_defensive_hard(self):
        # 입력 전제(dup 0) 방어 — manifest에 같은 date 2개 → silent 흡수 대신 hard
        d = date(2026, 6, 10)
        res = B.compare_manifest_with_existing([_manifest_row(d), _manifest_row(d)], [], _WS, _WE)
        self.assertTrue(any("manifest duplicate" in i for i in res.hard_issues))

    def test_existing_duplicate_defensive_hard(self):
        # DB unique 보장하지만 local/mock/query 이상 조기 차단
        d = date(2026, 6, 10)
        res = B.compare_manifest_with_existing([], [_existing_dict(d), _existing_dict(d)], _WS, _WE)
        self.assertTrue(any("existing duplicate" in i for i in res.hard_issues))


class TestQueryCompareRoundtrip(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import SourceDailyRate
        self.SourceDailyRate = SourceDailyRate
        self.engine = create_engine("sqlite:///:memory:")
        SourceDailyRate.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def _insert(self, db, d, close="1496.5", source="krx",
                asset="usd-krw-futures", contract_code="A75606"):
        db.add(self.SourceDailyRate(
            source=source, asset=asset, date_kst=d,
            rate=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close),
            ohlc_quality="source_ohlc", close_basis="krx_cf_close_1545",
            source_method="kis_daily_backfill", contract_code=contract_code,
            basis_date=None, published_at=None,
            metadata_json={"contract_short_code": contract_code, "contract_month": "202606",
                           "contract_expiry_date": "2026-06-15", "open": close},
        ))
        db.commit()

    def test_roundtrip_numeric_null_json_match(self):
        db = self.Session()
        self._insert(db, date(2026, 6, 10), close="1496.5")
        existing = B.query_existing_krx_rows(db, _WS, _WE)
        self.assertEqual(len(existing), 1)
        self.assertIsInstance(existing[0]["close"], float)        # Numeric → float round-trip
        self.assertIsNone(existing[0]["basis_date"])              # null 보존
        self.assertEqual(existing[0]["metadata_json"]["open"], "1496.5")  # JSON round-trip
        # manifest Decimal vs DB-roundtrip float → 일치
        res = B.compare_manifest_with_existing(
            [_manifest_row(date(2026, 6, 10), close="1496.5")], existing, _WS, _WE
        )
        self.assertEqual(res.hard_issues, [])
        self.assertEqual(len(res.matched_existing), 1)
        db.close()

    def test_query_filters_other_source_asset(self):
        db = self.Session()
        self._insert(db, date(2026, 6, 10), source="krx", asset="usd-krw-futures")
        self._insert(db, date(2026, 6, 11), source="bithumb", asset="usdt-krw", contract_code="X")
        existing = B.query_existing_krx_rows(db, _WS, _WE)
        self.assertEqual(len(existing), 1)  # KRX/usd-krw-futures만
        self.assertEqual(existing[0]["source"], "krx")
        db.close()

    def test_query_filters_date_window(self):
        db = self.Session()
        self._insert(db, date(2026, 6, 10))           # window 안
        self._insert(db, date(2026, 1, 1))            # window 밖 (date_kst 필터로 제외)
        existing = B.query_existing_krx_rows(db, _WS, _WE)
        self.assertEqual([r["date_kst"] for r in existing], [date(2026, 6, 10)])
        db.close()


if __name__ == "__main__":
    unittest.main()
