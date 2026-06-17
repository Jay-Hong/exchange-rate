"""P1b A2-4 — revision primitives + internal selector 단위 테스트 (§16 test ①②⑦).

conftest.py가 firebase stub + DATABASE_URL=sqlite 처리. 외부 DB/firebase 의존 0.
④⑤⑥(rollback/commit-fail/downstream-0)은 orchestrator 재구성이라 A3 영역 — A2-4 미포함.

검증:
- canonical 단위: epoch 0 / 음수 / naive=UTC / aware 비-UTC 변환 / μs 보존 / float .timestamp() 부재
- atomic_revision stdlib-only (app import 0 — cycle 회피)
- revision_from_row / StagedRateChange.revision = (canonical_epoch_us, id)
- internal selector revision == (canonical(record.timestamp), record.id), tie-break(id DESC),
  flush→requery 동일(②), session close 후 plain tuple 유효(⑦)
- 공개 selector dict shape 무변경 (id/revision 미노출)
"""
from __future__ import annotations

import pathlib
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import atomic_revision, crud, models


def _mem_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.BankExchangeRate.__table__.create(engine)
    models.InvestingExchangeRate.__table__.create(engine)
    return sessionmaker(bind=engine)()


class TestCanonicalEpochUs(unittest.TestCase):

    def test_unix_epoch_is_zero(self):
        self.assertEqual(
            atomic_revision.to_canonical_epoch_us(datetime(1970, 1, 1, tzinfo=timezone.utc)), 0
        )

    def test_microsecond_after_epoch(self):
        dt = datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(atomic_revision.to_canonical_epoch_us(dt), 1)

    def test_negative_epoch_before_1970(self):
        # codex plan-review case: 1969-12-31 23:59:59.999999 UTC → -1
        dt = datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
        self.assertEqual(atomic_revision.to_canonical_epoch_us(dt), -1)

    def test_naive_assumed_utc(self):
        naive = datetime(2026, 6, 17, 1, 2, 3, 456789)
        aware_utc = naive.replace(tzinfo=timezone.utc)
        self.assertEqual(
            atomic_revision.to_canonical_epoch_us(naive),
            atomic_revision.to_canonical_epoch_us(aware_utc),
        )

    def test_aware_non_utc_converted(self):
        kst = timezone(timedelta(hours=9))
        dt_kst = datetime(2026, 6, 17, 9, 0, 0, tzinfo=kst)        # == UTC 00:00
        dt_utc = datetime(2026, 6, 17, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            atomic_revision.to_canonical_epoch_us(dt_kst),
            atomic_revision.to_canonical_epoch_us(dt_utc),
        )

    def test_one_second_is_million_us(self):
        a = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        b = datetime(2000, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(
            atomic_revision.to_canonical_epoch_us(b) - atomic_revision.to_canonical_epoch_us(a),
            1_000_000,
        )

    def test_microsecond_preserved_not_truncated(self):
        dt = datetime(2026, 6, 17, 12, 34, 56, 789012, tzinfo=timezone.utc)
        self.assertEqual(atomic_revision.to_canonical_epoch_us(dt) % 1_000_000, 789012)


class TestModuleStdlibOnly(unittest.TestCase):

    def test_no_float_timestamp_call(self):
        # AST로 실제 `.timestamp()` 호출만 검사 — docstring/comment 문자열 false-positive 회피
        # (string scan이 §16 금지 설명 문구 자체를 잡던 문제).
        import ast

        tree = ast.parse(pathlib.Path(atomic_revision.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(
                    node.func.attr, "timestamp",
                    "float .timestamp() 호출 금지 (§16 μs rounding) — timedelta 산술만 사용",
                )

    def test_module_is_stdlib_only(self):
        src = pathlib.Path(atomic_revision.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from app", src, "atomic_revision는 stdlib-only — app import 0 (cycle 회피)")
        self.assertNotIn("import app", src, "atomic_revision는 stdlib-only")


class TestRevisionFromRow(unittest.TestCase):

    def test_revision_tuple_ints(self):
        class _Row:
            id = 42
            timestamp = datetime(2026, 6, 17, 0, 0, 0, tzinfo=timezone.utc)

        rev = atomic_revision.revision_from_row(_Row())
        self.assertEqual(rev, (atomic_revision.to_canonical_epoch_us(_Row.timestamp), 42))
        self.assertIsInstance(rev[0], int)
        self.assertIsInstance(rev[1], int)

    def test_id_zero_is_valid(self):
        # 경계: id=0은 유효 정수 — `not row.id` falsy 오거부 금지
        class _Row:
            id = 0
            timestamp = datetime(2026, 6, 17, 0, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(atomic_revision.revision_from_row(_Row())[1], 0)

    def test_none_id_raises_fail_closed(self):
        # flush 전 ORM row(id=None) → fail-closed (조용히 (epoch, None) 반환 금지, §16)
        class _Row:
            id = None
            timestamp = datetime(2026, 6, 17, 0, 0, 0, tzinfo=timezone.utc)

        with self.assertRaises(ValueError):
            atomic_revision.revision_from_row(_Row())

    def test_unflushed_orm_row_raises(self):
        # 실제 SQLAlchemy pending row(flush 전 id=None) → ValueError
        row = models.BankExchangeRate(
            bank="kb", currency="usd-krw", rate=1300.0, timestamp=datetime(2026, 6, 17, 0, 0, 0)
        )
        self.assertIsNone(row.id)  # flush 전
        with self.assertRaises(ValueError):
            atomic_revision.revision_from_row(row)


class TestStagedRateChange(unittest.TestCase):

    def test_revision_property_matches_row(self):
        class _Row:
            id = 7
            timestamp = datetime(2026, 6, 17, 1, 2, 3, 4, tzinfo=timezone.utc)

        change = crud.ChangedRate(source="kb", asset="usd-krw", rate=1300.0, changed_at=_Row.timestamp)
        staged = crud.StagedRateChange(change=change, row=_Row())
        self.assertEqual(staged.revision, (atomic_revision.to_canonical_epoch_us(_Row.timestamp), 7))

    def test_revision_property_fail_closed_before_flush(self):
        # StagedRateChange.revision은 revision_from_row 경유 — flush 전 id=None이면 fail-closed
        row = models.BankExchangeRate(
            bank="kb", currency="usd-krw", rate=1300.0, timestamp=datetime(2026, 6, 17, 0, 0, 0)
        )
        change = crud.ChangedRate(source="kb", asset="usd-krw", rate=1300.0, changed_at=row.timestamp)
        staged = crud.StagedRateChange(change=change, row=row)
        with self.assertRaises(ValueError):
            _ = staged.revision


class TestInternalSelectorRevision(unittest.TestCase):

    def test_bank_selector_revision_matches_row(self):
        s = _mem_session()
        row = models.BankExchangeRate(
            bank="kb", currency="usd-krw", rate=1300.0, timestamp=datetime(2026, 6, 17, 0, 0, 0, 123456)
        )
        s.add(row)
        s.commit()
        result = crud._select_latest_bank_rates_with_revision(s, "usd-krw")
        self.assertEqual(len(result), 1)
        rr = result[0]
        self.assertEqual((rr.source, rr.asset, rr.rate), ("kb", "usd-krw", 1300.0))
        self.assertEqual(rr.revision, (atomic_revision.to_canonical_epoch_us(row.timestamp), row.id))

    def test_investing_selector_revision_matches_row(self):
        s = _mem_session()
        row = models.InvestingExchangeRate(
            currency="usd-krw", rate=1305.0, timestamp=datetime(2026, 6, 17, 0, 0, 0, 654321)
        )
        s.add(row)
        s.commit()
        rr = crud._select_latest_investing_rate_with_revision(s, "usd-krw")
        self.assertIsNotNone(rr)
        self.assertEqual(rr.source, "investing")
        self.assertEqual(rr.revision, (atomic_revision.to_canonical_epoch_us(row.timestamp), row.id))

    def test_investing_selector_none_when_empty(self):
        self.assertIsNone(crud._select_latest_investing_rate_with_revision(_mem_session(), "usd-krw"))

    def test_flush_requery_revision_identical(self):
        # ② direct(flush-row-ref) revision == selector(requery) revision — §16 핵심 invariant
        s = _mem_session()
        row = models.BankExchangeRate(
            bank="hana", currency="usd-krw", rate=1301.0, timestamp=datetime(2026, 6, 17, 3, 4, 5, 111213)
        )
        s.add(row)
        s.flush()
        direct_rev = atomic_revision.revision_from_row(row)
        s.commit()
        selector_rev = crud._select_latest_bank_rates_with_revision(s, "usd-krw")[0].revision
        self.assertEqual(direct_rev, selector_rev)

    def test_same_timestamp_tie_break_higher_id(self):
        s = _mem_session()
        ts = datetime(2026, 6, 17, 5, 0, 0, 0)
        r1 = models.BankExchangeRate(bank="kb", currency="usd-krw", rate=1300.0, timestamp=ts)
        r2 = models.BankExchangeRate(bank="kb", currency="usd-krw", rate=1302.0, timestamp=ts)
        s.add_all([r1, r2])
        s.commit()
        result = crud._select_latest_bank_rates_with_revision(s, "usd-krw")
        self.assertEqual(len(result), 1)                       # kb 1건
        self.assertEqual(result[0].revision[1], max(r1.id, r2.id))  # id DESC tie-break
        self.assertEqual(result[0].rate, 1302.0)

    def test_revision_plain_tuple_valid_after_session_close(self):
        # ⑦ revision은 plain int tuple — session close 후 lazy-load 없이 유효
        s = _mem_session()
        s.add(models.BankExchangeRate(
            bank="kb", currency="usd-krw", rate=1300.0, timestamp=datetime(2026, 6, 17, 6, 0, 0, 7)
        ))
        s.commit()
        rev = crud._select_latest_bank_rates_with_revision(s, "usd-krw")[0].revision
        s.close()
        self.assertIsInstance(rev[0], int)
        self.assertIsInstance(rev[1], int)


class TestPublicSelectorNoLeak(unittest.TestCase):
    """A2-4가 공개 selector를 변경하지 않음 — dict shape에 id/revision 미노출 유지."""

    def test_bank_public_selector_keys_unchanged(self):
        s = _mem_session()
        s.add(models.BankExchangeRate(
            bank="kb", currency="usd-krw", rate=1300.0, timestamp=datetime(2026, 6, 17, 0, 0, 0)
        ))
        s.commit()
        out = crud.select_latest_bank_rates_from_db(s, "usd-krw")
        self.assertEqual(set(out[0].keys()), {"currency", "bank", "rate", "timestamp"})

    def test_investing_public_selector_keys_unchanged(self):
        s = _mem_session()
        s.add(models.InvestingExchangeRate(
            currency="usd-krw", rate=1305.0, timestamp=datetime(2026, 6, 17, 0, 0, 0)
        ))
        s.commit()
        out = crud.select_a_latest_investing_rate_from_db(s, "usd-krw")
        self.assertEqual(set(out.keys()), {"currency", "bank", "rate", "timestamp"})


if __name__ == "__main__":
    unittest.main()
