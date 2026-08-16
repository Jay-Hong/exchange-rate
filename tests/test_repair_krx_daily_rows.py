"""scripts/repair_krx_daily_rows.py 단위 테스트 — DB·네트워크 의존 0.

`repair_one`의 IO는 전부 주입한다. 순서 계약(사전 조건 → fresh fetch → 검증 →
write → post-verify)과 abort 조건을 잠근다.

실행:
    python -m pytest tests/test_repair_krx_daily_rows.py -p no:asyncio -q
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import date

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import repair_krx_daily_rows as R  # noqa: E402

FEB = date(2026, 2, 13)
AUG = date(2026, 8, 14)


def _row(contract="A75603", close="1441.0", high="1445.0", low="1438.0",
         d=FEB, **over) -> dict:
    base = {
        "date_kst": d, "contract_code": contract,
        "rate": close, "close": close, "high": high, "low": low,
        "close_basis": R.CLOSE_BASIS, "source_method": R.SOURCE_METHOD,
        "ohlc_quality": R.OHLC_QUALITY, "basis_date": None, "published_at": None,
    }
    base.update(over)
    return base


class TestAllowlist(unittest.TestCase):

    def test_only_two_dates_allowed(self):
        self.assertEqual({t.date_kst for t in R.REPAIR_ALLOWLIST}, {FEB, AUG})

    def test_feb_is_update_aug_is_insert(self):
        feb = R.resolve_target(FEB)
        aug = R.resolve_target(AUG)
        self.assertEqual((feb.action, feb.expected_existing_contract,
                          feb.target_contract), ("update", "A75602", "A75603"))
        self.assertEqual((aug.action, aug.expected_existing_contract,
                          aug.target_contract), ("insert", None, "A75609"))

    def test_other_dates_abort(self):
        """CLI로도 확장 불가 — allowlist는 코드에 고정."""
        for bad in (date(2026, 2, 12), date(2026, 8, 17), date(2026, 5, 18)):
            with self.subTest(bad=bad):
                with self.assertRaises(R.RepairAbort):
                    R.resolve_target(bad)


class TestPrecondition(unittest.TestCase):

    def test_update_requires_exact_existing_contract(self):
        target = R.resolve_target(FEB)
        R.check_precondition(target, _row(contract="A75602"))  # 통과
        with self.assertRaises(R.RepairAbort):
            R.check_precondition(target, _row(contract="A75603"))  # 이미 교정됨
        with self.assertRaises(R.RepairAbort):
            R.check_precondition(target, None)  # 행 부재

    def test_insert_requires_absence(self):
        target = R.resolve_target(AUG)
        R.check_precondition(target, None)  # 통과
        with self.assertRaises(R.RepairAbort):
            R.check_precondition(target, _row(d=AUG))

    def test_already_repaired_aborts_instead_of_silent_skip(self):
        """'이미 고쳐져 있으면 skip'으로 만들면 **다른 무언가가 바꿔 놓은 경우**와
        구분되지 않는다. 일회성 교정이므로 상태가 다르면 사람이 봐야 한다."""
        with self.assertRaises(R.RepairAbort) as ctx:
            R.check_precondition(R.resolve_target(FEB), _row(contract="A75603"))
        self.assertIn("이미 교정", str(ctx.exception))


class TestFetchedRowValidation(unittest.TestCase):

    def test_valid_row_passes(self):
        R.validate_fetched_row(R.resolve_target(FEB), _row())

    def test_wrong_contract_aborts(self):
        with self.assertRaises(R.RepairAbort):
            R.validate_fetched_row(R.resolve_target(FEB), _row(contract="A75602"))

    def test_wrong_date_aborts(self):
        with self.assertRaises(R.RepairAbort):
            R.validate_fetched_row(R.resolve_target(FEB), _row(d=date(2026, 2, 12)))

    def test_missing_ohlc_aborts(self):
        for field in ("close", "high", "low"):
            with self.subTest(field=field):
                with self.assertRaises(R.RepairAbort):
                    R.validate_fetched_row(R.resolve_target(FEB), _row(**{field: None}))

    def test_ohlc_order_violation_aborts(self):
        with self.assertRaises(R.RepairAbort):
            R.validate_fetched_row(
                R.resolve_target(FEB), _row(close="1500.0", high="1445.0"))

    def test_nonpositive_aborts(self):
        with self.assertRaises(R.RepairAbort):
            R.validate_fetched_row(R.resolve_target(FEB), _row(low="0"))

    def test_rate_close_mismatch_aborts(self):
        with self.assertRaises(R.RepairAbort):
            R.validate_fetched_row(R.resolve_target(FEB), _row(rate="1442.0"))


class TestPostVerify(unittest.TestCase):

    def test_valid_post_state_passes(self):
        R.verify_post_state(R.resolve_target(FEB), _row())

    def test_missing_row_aborts(self):
        with self.assertRaises(R.RepairAbort):
            R.verify_post_state(R.resolve_target(FEB), None)

    def test_wrong_contract_aborts(self):
        with self.assertRaises(R.RepairAbort):
            R.verify_post_state(R.resolve_target(FEB), _row(contract="A75602"))

    def test_provenance_fields_locked(self):
        for field, bad in (("source_method", "close_finalizer"),
                           ("close_basis", "hana_observed_eod"),
                           ("ohlc_quality", "observed_rollup")):
            with self.subTest(field=field):
                with self.assertRaises(R.RepairAbort):
                    R.verify_post_state(R.resolve_target(FEB), _row(**{field: bad}))

    def test_krx_nullable_fields_must_stay_none(self):
        """KRX 방향은 basis_date·published_at이 None이어야 한다.

        upsert가 nullable을 보존하므로, official/observed 행과 겹치면 leftover가
        남아 여기서 걸린다 (Hana overlap guard와 같은 안전망 축)."""
        for field in ("basis_date", "published_at"):
            with self.subTest(field=field):
                with self.assertRaises(R.RepairAbort):
                    R.verify_post_state(R.resolve_target(FEB),
                                        _row(**{field: date(2026, 2, 12)}))

    def test_rate_close_invariant(self):
        with self.assertRaises(R.RepairAbort):
            R.verify_post_state(R.resolve_target(FEB), _row(rate="1440.0"))

    def test_fetched_values_must_be_reflected(self):
        """⭐ contract만 바꾸고 기존 가격을 남긴 write를 잡는다.

        contract·provenance·`rate==close`만 보면, 기존 행도 `rate==close`이므로
        **가격이 그대로여도 통과**한다 (codex 리뷰 지적). fetched와 대조한다.
        """
        target = R.resolve_target(FEB)
        fetched = _row(close="1443.5", high="1446.0", low="1440.0")
        stale_price = _row(close="1441.0", high="1445.0", low="1438.0")
        # contract·provenance·invariant는 전부 정상인데 값만 옛것
        R.verify_post_state(target, stale_price)  # fetched 미주입이면 통과(구 동작)
        with self.assertRaises(R.RepairAbort) as ctx:
            R.verify_post_state(target, stale_price, fetched)
        self.assertIn("반영하지 않았다", str(ctx.exception))

    def test_reflected_values_pass(self):
        target = R.resolve_target(FEB)
        fetched = _row(close="1443.5", high="1446.0", low="1440.0")
        R.verify_post_state(target, dict(fetched), fetched)


class TestPreimageFingerprint(unittest.TestCase):

    def test_captures_absence_and_presence(self):
        fp = R.preimage_fingerprint({FEB: _row(contract="A75602"), AUG: None})
        self.assertIn("A75602", fp)
        self.assertIn("null", fp)

    def test_stable_across_key_order(self):
        a = R.preimage_fingerprint({FEB: _row(), AUG: None})
        b = R.preimage_fingerprint({AUG: None, FEB: _row()})
        self.assertEqual(a, b)

    def test_changes_when_contract_changes(self):
        self.assertNotEqual(
            R.preimage_fingerprint({FEB: _row(contract="A75602")}),
            R.preimage_fingerprint({FEB: _row(contract="A75603")}),
        )


class TestProductionGuard(unittest.TestCase):

    def test_dry_run_never_blocked(self):
        R.check_production_write_guard("postgresql+psycopg://x", write=False,
                                       allow_production=False)

    def test_production_write_requires_explicit_flag(self):
        with self.assertRaises(R.RepairAbort):
            R.check_production_write_guard("postgresql+psycopg://x", write=True,
                                           allow_production=False)

    def test_production_write_allowed_with_flag(self):
        R.check_production_write_guard("postgresql+psycopg://x", write=True,
                                       allow_production=True)

    def test_sqlite_write_not_blocked(self):
        R.check_production_write_guard("sqlite:///tmp.db", write=True,
                                       allow_production=False)


class TestRepairOneOrdering(unittest.TestCase):
    """순서 계약: 사전 조건 → fresh fetch → 검증 → write → post-verify."""

    def _harness(self, existing_before, fetched, *, existing_after=None):
        calls: list[str] = []
        state = {"row": existing_before}

        def read_existing(_d):
            calls.append("read")
            return state["row"]

        def fetch_official(_t):
            calls.append("fetch")
            return fetched

        def write_row(_t, row):
            calls.append("write")
            state["row"] = existing_after if existing_after is not None else row

        return calls, read_existing, fetch_official, write_row

    def test_update_happy_path(self):
        calls, r, f, w = self._harness(_row(contract="A75602"), _row())
        got = R.repair_one(R.resolve_target(FEB), read_existing=r,
                           fetch_official=f, write_row=w)
        self.assertEqual(calls, ["read", "fetch", "write", "read"])
        self.assertEqual(got["contract_code"], "A75603")

    def test_insert_happy_path(self):
        fetched = _row(contract="A75609", d=AUG, close="1417.8",
                       high="1420.6", low="1413.1")
        calls, r, f, w = self._harness(None, fetched)
        got = R.repair_one(R.resolve_target(AUG), read_existing=r,
                           fetch_official=f, write_row=w)
        self.assertEqual(got["action"], "insert")
        self.assertEqual(calls, ["read", "fetch", "write", "read"])

    def test_precondition_failure_skips_fetch_entirely(self):
        """abort할 상태에서 외부 API를 때리지 않는다 (순서가 계약인 이유)."""
        calls, r, f, w = self._harness(_row(contract="A75603"), _row())
        with self.assertRaises(R.RepairAbort):
            R.repair_one(R.resolve_target(FEB), read_existing=r,
                         fetch_official=f, write_row=w)
        self.assertEqual(calls, ["read"])
        self.assertNotIn("fetch", calls)
        self.assertNotIn("write", calls)

    def test_bad_fetch_aborts_before_write(self):
        calls, r, f, w = self._harness(_row(contract="A75602"),
                                       _row(contract="A75602"))
        with self.assertRaises(R.RepairAbort):
            R.repair_one(R.resolve_target(FEB), read_existing=r,
                         fetch_official=f, write_row=w)
        self.assertEqual(calls, ["read", "fetch"])
        self.assertNotIn("write", calls)

    def test_post_verify_failure_propagates_for_rollback(self):
        """write가 됐어도 post-verify 실패면 예외를 올려 caller가 rollback한다."""
        calls, r, f, w = self._harness(
            _row(contract="A75602"), _row(),
            existing_after=_row(contract="A75602"),  # write가 반영 안 된 상태
        )
        with self.assertRaises(R.RepairAbort):
            R.repair_one(R.resolve_target(FEB), read_existing=r,
                         fetch_official=f, write_row=w)
        self.assertEqual(calls, ["read", "fetch", "write", "read"])

    def test_contract_only_write_is_caught_by_repair_one(self):
        """contract만 갈아끼우고 가격을 남긴 write를 end-to-end로 잡는다."""
        fetched = _row(close="1443.5", high="1446.0", low="1440.0")
        stale = _row(contract="A75603", close="1441.0", high="1445.0", low="1438.0")
        calls, r, f, w = self._harness(_row(contract="A75602"), fetched,
                                       existing_after=stale)
        with self.assertRaises(R.RepairAbort) as ctx:
            R.repair_one(R.resolve_target(FEB), read_existing=r,
                         fetch_official=f, write_row=w)
        self.assertIn("반영하지 않았다", str(ctx.exception))

    def test_fetch_is_fresh_each_run(self):
        """dry-run 산출물 재사용 금지 — 매 실행이 fetch를 부른다."""
        for _ in range(2):
            calls, r, f, w = self._harness(_row(contract="A75602"), _row())
            R.repair_one(R.resolve_target(FEB), read_existing=r,
                         fetch_official=f, write_row=w)
            self.assertEqual(calls.count("fetch"), 1)


if __name__ == "__main__":
    unittest.main()
