"""IBK 결과 판정 어댑터 — '공식값을 읽었다'와 'DB에 안전하게 반영됐다'의 분리 검증.

핵심 사례는 임시 SQLite 파일과 실제 crud writer를 쓴다. Redis/FCM/topic 등 외부 부수효과만
차단한다. 이 통과는 로컬 SQLite 경계의 검증이며 운영 PostgreSQL·실제 HTTP·Selenium 검증이 아니다.
"""

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import atomic_write_runtime as awr
from app import crud, models
# 테스트는 crawlers를 import해도 순환이 아니다. builder 자신은 import하지 않는다.
from app.crawlers.constants import MIBANK_RATE_RANGES
from app.ibk_result_builder import (
    REQUIRED_PAIRS,
    IbkDbSnapshot,
    IbkIntendedState,
    IbkObservationFailure,
    IbkOfficialObservation,
    IbkPreservationGrant,
    IbkResultBuildError,
    IbkRetentionReason,
    IbkWriteAttempt,
    IbkWriteMode,
    build_ibk_result,
    capture_write_mode,
    read_ibk_db_snapshot,
)
from app.ibk_result_protocol import (
    IbkProtocolError,
    IbkReason,
    IbkSource,
    IbkStatus,
    decode_ibk_result,
    encode_ibk_result,
)
from app.ibk_run_context import IbkRunContext

KST = timezone(timedelta(hours=9))
BANK = "ibk"
RUN_ID = "a" * 32
DAY = date(2026, 9, 7)
COMPLETED = datetime(2026, 9, 7, 8, 30, 35, tzinfo=KST)
NOW = datetime(2026, 9, 7, 8, 30, 40, tzinfo=KST)
OFFICIAL = {"usd-krw": 1390.5, "jpy-krw": 950.25, "eur-krw": 1620.0}
OLD = {"usd-krw": 1380.0, "jpy-krw": 940.0, "eur-krw": 1600.0}


def _observation(rates=None, day=DAY, source=IbkSource.OFFICIAL_POST):
    return IbkOfficialObservation(source, day, dict(rates or OFFICIAL), COMPLETED)


def _submitted_all(rates=None):
    """3통화를 모두 제출하기로 한 의도."""
    return IbkIntendedState(submitted=dict(rates or OFFICIAL))


def _retained_all(rates):
    """공식 후보 없이 기존 DB 3통화를 모두 유지하기로 한 의도."""
    return IbkIntendedState(retained=dict(rates),
                            retention={pair: IbkRetentionReason.NOT_OBSERVED for pair in rates})


def _build(**kwargs):
    kwargs.setdefault("run_id", RUN_ID)
    kwargs.setdefault("expected_service_date", DAY)
    kwargs.setdefault("observed_at", NOW)
    return build_ibk_result(**kwargs)


class RealDbBuilderTest(unittest.TestCase):
    """실제 SQLite 파일 + 실제 crud writer. 별 세션에서도 결과를 확인한다."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = handle.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        self.engine = create_engine(f"sqlite:///{self.path}")
        models.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        for name, value in (("_write_changed_bank_rates_to_redis", []),
                            ("process_rate_alerts", 0),
                            ("_emit_fx_alert_canary", False),
                            ("_emit_fx_alert_shadow", None),
                            ("_emit_topic_triggers", None)):
            patcher = patch.object(crud, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self, rates):
        """쓰기 시점 게이트를 캡처하고 기존 writer로 저장한다."""
        mode = capture_write_mode()
        count = crud.insert_bank_rates_into_db(db=self.db, current_rates=dict(rates), bank_name=BANK)
        return IbkWriteAttempt(True, mode, count)

    def _snapshot(self, session=None):
        return read_ibk_db_snapshot(session or self.Session(), bank_name=BANK, ranges=MIBANK_RATE_RANGES)

    def _rows(self):
        with self.Session() as fresh:
            return fresh.query(models.BankExchangeRate).count()

    def test_1_empty_db_official_write_then_match_is_observed(self):
        write = self._write(OFFICIAL)
        self.assertEqual(write.returned_count, 3)
        result = _build(db_after=self._snapshot(), official=_observation(), intended=_submitted_all(), write=write)
        self.assertIs(result.status, IbkStatus.OBSERVED)
        self.assertIs(result.reason, IbkReason.NORMAL)
        self.assertEqual(result.changed_count, 3)
        self.assertTrue(result.db_snapshot_complete)
        self.assertEqual(result.preserved_pairs, REQUIRED_PAIRS)
        self.assertEqual(result.missing_pairs, ())
        self.assertEqual(result.official_completed_at, COMPLETED.isoformat())

    def test_2_unchanged_complete_db_returns_zero_and_is_still_observed(self):
        self._write(OFFICIAL)
        write = self._write(OFFICIAL)
        self.assertEqual(write.returned_count, 0)
        self.assertIs(write.write_mode, IbkWriteMode.LEGACY)
        result = _build(db_after=self._snapshot(), official=_observation(), intended=_submitted_all(), write=write)
        self.assertIs(result.status, IbkStatus.OBSERVED)
        self.assertEqual(result.changed_count, 0)

    def test_3_real_uninitialized_guard_zero_is_not_read_as_unchanged(self):
        awr._reset_for_test()
        self.assertFalse(awr.is_initialized())
        write = self._write(OFFICIAL)
        self.assertIs(write.write_mode, IbkWriteMode.BLOCKED_UNINITIALIZED)
        self.assertEqual(write.returned_count, 0)
        self.assertEqual(self._rows(), 0)  # 실제로 한 줄도 저장되지 않았다
        blocked = _build(db_after=self._snapshot(), official=_observation(), intended=_submitted_all(), write=write)
        self.assertIs(blocked.status, IbkStatus.FAILED)
        self.assertIs(blocked.reason, IbkReason.WRITE_POLICY_BLOCKED)
        # 같은 반환 0이라도 게이트가 legacy이고 DB가 일치하면 판정이 다르다.
        self.assertIs(IbkWriteMode.LEGACY.blocked, False)

    def test_4_blocked_with_usable_but_different_existing_db_is_degraded(self):
        self._write(OLD)
        awr._reset_for_test()
        write = self._write(OFFICIAL)
        self.assertTrue(write.blocked)
        result = _build(db_after=self._snapshot(), official=_observation(), intended=_submitted_all(), write=write)
        self.assertIs(result.status, IbkStatus.DEGRADED)
        self.assertIs(result.reason, IbkReason.WRITE_POLICY_BLOCKED)
        self.assertTrue(result.db_snapshot_complete)

    def test_5_blocked_with_partial_or_empty_db_is_failed(self):
        self._write({"usd-krw": OLD["usd-krw"]})
        awr._reset_for_test()
        write = self._write(OFFICIAL)
        snapshot = self._snapshot()
        self.assertEqual(snapshot.usable, ("usd-krw",))
        result = _build(db_after=snapshot, official=_observation(), intended=_submitted_all(), write=write)
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.WRITE_POLICY_BLOCKED)
        self.assertFalse(result.db_snapshot_complete)

    def test_5b_halt_gate_is_also_recorded_as_blocked(self):
        awr._current = awr.WriteModeSnapshot(
            diagnostic_effective_mode="halt", activation_latched=False,
            enforced_action="halt", mode_generation=0)
        write = self._write(OFFICIAL)
        self.assertIs(write.write_mode, IbkWriteMode.BLOCKED_ENFORCED)
        self.assertEqual(write.returned_count, 0)
        self.assertEqual(self._rows(), 0)
        result = _build(db_after=self._snapshot(), official=_observation(), intended=_submitted_all(), write=write)
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.WRITE_POLICY_BLOCKED)

    def test_5c_blocked_but_db_already_matches_is_still_observed(self):
        self._write(OFFICIAL)                      # 정상 게이트로 먼저 적재
        awr._reset_for_test()
        blocked = self._write(OFFICIAL)            # 차단 상태에서 같은 값 재시도
        self.assertTrue(blocked.blocked)
        self.assertEqual(blocked.returned_count, 0)
        result = _build(db_after=self._snapshot(), official=_observation(), intended=_submitted_all(), write=blocked)
        # 필요한 값이 이미 반영돼 있으므로 차단이 관측을 부정하지 않는다.
        self.assertIs(result.status, IbkStatus.OBSERVED)
        self.assertIs(result.reason, IbkReason.NORMAL)

    def test_5d_catch_up_write_that_did_not_land_is_not_preserved(self):
        self._write(OLD)
        write = IbkWriteAttempt(True, IbkWriteMode.LEGACY, 3)
        grant = IbkPreservationGrant(IbkReason.PREOPEN_PENDING)
        result = _build(db_after=self._snapshot(), official=_observation(day=date(2026, 9, 4)),
                        preservation=grant, intended=_submitted_all(), write=write)
        self.assertIs(result.status, IbkStatus.DEGRADED)
        self.assertIs(result.reason, IbkReason.DB_APPLY_MISMATCH)

    def test_5e_validated_past_candidate_keeps_its_metadata_under_preservation(self):
        write = self._write(OFFICIAL)
        past = IbkOfficialObservation(IbkSource.OFFICIAL_POST, date(2026, 9, 4), dict(OFFICIAL),
                                      datetime(2026, 9, 4, 18, 0, tzinfo=KST))
        result = _build(db_after=self._snapshot(), official=past,
                        preservation=IbkPreservationGrant(IbkReason.PREOPEN_PENDING),
                        intended=_submitted_all(), write=write)
        self.assertIs(result.status, IbkStatus.PRESERVED)
        self.assertIs(result.reason, IbkReason.PREOPEN_PENDING)
        self.assertEqual(result.observed_service_date, "2026-09-04")
        self.assertEqual(result.official_completed_at, datetime(2026, 9, 4, 18, 0, tzinfo=KST).isoformat())
        self.assertEqual(result.observed_pairs, REQUIRED_PAIRS)
        self.assertEqual(result.changed_count, 3)

    def test_6_positive_count_with_mismatched_reread_is_not_promoted(self):
        write = self._write(OFFICIAL)
        self.assertEqual(write.returned_count, 3)
        drifted = IbkDbSnapshot(True, dict(OLD), REQUIRED_PAIRS, ())
        result = _build(db_after=drifted, official=_observation(), intended=_submitted_all(), write=write)
        self.assertIs(result.status, IbkStatus.DEGRADED)
        # 재조회는 됐으나 의도한 값과 다름 — DB가 고장났다고 단정하지 않는다.
        self.assertIs(result.reason, IbkReason.DB_APPLY_MISMATCH)
        unknown = _build(db_after=IbkDbSnapshot.unknown(), official=_observation(), intended=_submitted_all(), write=write)
        self.assertIs(unknown.status, IbkStatus.FAILED)
        self.assertIs(unknown.reason, IbkReason.DB_ERROR)  # 조회 자체가 실패한 경우와 구분

    def test_7_read_exception_is_distinct_from_empty_db(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError("db down")

        failed = read_ibk_db_snapshot(self.db, bank_name=BANK, ranges=MIBANK_RATE_RANGES, reader=boom)
        empty = self._snapshot()
        self.assertFalse(failed.checked)
        self.assertIsNone(failed.complete)
        self.assertIsNone(failed.usable)
        self.assertTrue(empty.checked)
        self.assertIs(empty.complete, False)
        self.assertEqual(empty.usable, ())
        self.assertEqual(empty.missing, REQUIRED_PAIRS)

    def test_8_commit_failure_is_not_reported_as_stored(self):
        with patch.object(self.db, "commit", side_effect=RuntimeError("commit failed")):
            with self.assertRaises(RuntimeError):
                crud.insert_bank_rates_into_db(db=self.db, current_rates=dict(OFFICIAL), bank_name=BANK)
        self.db.rollback()
        self.assertEqual(self._rows(), 0)  # 별 세션에서도 저장되지 않았음을 확인
        write = IbkWriteAttempt(True, IbkWriteMode.LEGACY, None, failed=True)
        result = _build(db_after=self._snapshot(), official=_observation(), intended=_submitted_all(), write=write)
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.DB_ERROR)
        self.assertIsNone(result.changed_count)

    def test_9_observed_pairs_never_manufacture_db_completeness(self):
        awr._reset_for_test()
        write = self._write(OFFICIAL)
        result = _build(db_after=self._snapshot(), official=_observation(), intended=_submitted_all(), write=write)
        self.assertEqual(result.observed_pairs, REQUIRED_PAIRS)
        self.assertEqual(result.missing_pairs, REQUIRED_PAIRS)  # 관측과 누락은 겹칠 수 있다
        self.assertIs(result.db_snapshot_complete, False)

    def test_10_unusable_rows_are_not_counted_as_available(self):
        with self.Session() as seed:
            seed.add(models.BankExchangeRate(bank=BANK, currency="usd-krw", rate=99.0,
                                             timestamp=datetime(2026, 9, 7, 0, 0)))
            seed.add(models.BankExchangeRate(bank=BANK, currency="jpy-krw", rate=float("nan"),
                                             timestamp=datetime(2026, 9, 7, 0, 0)))
            seed.commit()
        snapshot = self._snapshot()
        self.assertEqual(snapshot.usable, ())  # 범위 밖·NaN 은 사용 가능이 아니다
        stub = {"usd-krw": {"rate": 1390.5, "timestamp": None},
                "jpy-krw": {"rate": float("inf"), "timestamp": datetime(2026, 9, 7)},
                "eur-krw": {"rate": None, "timestamp": datetime(2026, 9, 7)}}
        by_stub = read_ibk_db_snapshot(self.db, bank_name=BANK, ranges=MIBANK_RATE_RANGES,
                                       reader=lambda *_a, **_k: stub)
        self.assertEqual(by_stub.usable, ())
        self.assertIs(by_stub.complete, False)

    def test_11_change_only_storage_does_not_require_equal_timestamps(self):
        # change-only 저장이라 통화별 마지막 변경 시각이 서로 다른 것이 정상이다.
        # 실행 타이밍에 기대지 않고 서로 다른 시각을 명시적으로 심는다.
        with self.Session() as seed:
            for pair, offset in (("usd-krw", 0), ("jpy-krw", 3600), ("eur-krw", 7200)):
                seed.add(models.BankExchangeRate(
                    bank=BANK, currency=pair, rate=OFFICIAL[pair],
                    timestamp=datetime(2026, 9, 6, 12, 0) + timedelta(seconds=offset)))
            seed.commit()
        with self.Session() as fresh:
            rows = crud.get_last_bank_rates_with_ts(fresh, BANK, list(REQUIRED_PAIRS))
        self.assertEqual(len({rows[pair]["timestamp"] for pair in REQUIRED_PAIRS}), 3)
        snapshot = self._snapshot()
        self.assertEqual(snapshot.usable, REQUIRED_PAIRS)
        self.assertTrue(snapshot.complete)


    def test_12_partial_regression_guard_exclusion_is_not_normal_observation(self):
        self._write(OLD)                                    # 기존 DB
        kept = {pair: OLD[pair] for pair in ("jpy-krw", "eur-krw")}
        write = self._write({"usd-krw": OFFICIAL["usd-krw"]})   # usd만 보충
        state = IbkIntendedState(submitted={"usd-krw": OFFICIAL["usd-krw"]}, retained=kept,
                                 retention={pair: IbkRetentionReason.REGRESSION_GUARD for pair in kept})
        result = _build(db_after=self._snapshot(), official=_observation(),
                        intended=state, write=write)
        self.assertIs(result.status, IbkStatus.DEGRADED)
        self.assertIs(result.reason, IbkReason.REGRESSION_GUARD)

    def test_13_all_pairs_excluded_needs_no_crud_call(self):
        self._write(OLD)
        state = IbkIntendedState(retained=dict(OLD),
                                 retention={pair: IbkRetentionReason.REGRESSION_GUARD for pair in OLD})
        result = _build(db_after=self._snapshot(), official=_observation(),
                        intended=state, write=IbkWriteAttempt.none())
        self.assertIs(result.status, IbkStatus.DEGRADED)
        self.assertIs(result.reason, IbkReason.REGRESSION_GUARD)
        self.assertIsNone(result.changed_count)

    def test_14_changed_retained_value_is_detected(self):
        self._write(OLD)
        kept = {pair: OLD[pair] for pair in ("jpy-krw", "eur-krw")}
        write = self._write({"usd-krw": OFFICIAL["usd-krw"]})
        state = IbkIntendedState(submitted={"usd-krw": OFFICIAL["usd-krw"]}, retained=kept,
                                 retention={pair: IbkRetentionReason.REGRESSION_GUARD for pair in kept})
        settled = _build(db_after=self._snapshot(), official=_observation(), intended=state, write=write)
        self.assertIs(settled.reason, IbkReason.REGRESSION_GUARD)   # 대조군
        self.assertNotEqual(OLD["jpy-krw"], 935.0)                  # 실제로 달라지는 값인지 확인
        self._write({"jpy-krw": 935.0})                             # 유지하기로 한 값이 바뀜
        drifted = _build(db_after=self._snapshot(), official=_observation(), intended=state, write=write)
        self.assertIs(drifted.status, IbkStatus.DEGRADED)
        self.assertIs(drifted.reason, IbkReason.DB_APPLY_MISMATCH)


class RangeAndTimestampGateTest(unittest.TestCase):
    """범위 설정 누락이 검증 생략으로 바뀌지 않는지, timestamp 타입을 보는지 잠근다."""

    GOOD = {pair: {"rate": OFFICIAL[pair], "timestamp": datetime(2026, 9, 7, 0, 0)}
            for pair in REQUIRED_PAIRS}

    def _snap(self, rows, ranges=MIBANK_RATE_RANGES):
        return read_ibk_db_snapshot(None, bank_name=BANK, ranges=ranges,
                                    reader=lambda *_a, **_k: rows)

    def test_20_control_all_good_rows_are_complete(self):
        self.assertIs(self._snap(self.GOOD).complete, True)

    def test_21_missing_or_invalid_ranges_are_rejected(self):
        for ranges in ({}, {"usd-krw": (1000, 2000)}, dict(MIBANK_RATE_RANGES, **{"usd-krw": None}),
                       dict(MIBANK_RATE_RANGES, **{"usd-krw": (2000, 1000)}),
                       dict(MIBANK_RATE_RANGES, **{"usd-krw": (float("nan"), 2000)}), None):
            with self.assertRaises(IbkResultBuildError):
                self._snap(self.GOOD, ranges=ranges)

    def test_22_non_datetime_timestamp_is_not_usable(self):
        for stamp in ("not-a-datetime", 0, date(2026, 9, 7), True):
            rows = {pair: {"rate": OFFICIAL[pair], "timestamp": stamp} for pair in REQUIRED_PAIRS}
            snapshot = self._snap(rows)
            self.assertEqual(snapshot.usable, ())
            self.assertIs(snapshot.complete, False)

    def test_23_naive_utc_datetime_from_db_stays_usable(self):
        self.assertEqual(self._snap(self.GOOD).usable, REQUIRED_PAIRS)


class PureBuilderTest(unittest.TestCase):
    """DB 없이 판정 규칙과 입력 계약만 잠근다."""

    COMPLETE = IbkDbSnapshot(True, dict(OFFICIAL), REQUIRED_PAIRS, ())
    KEPT = IbkDbSnapshot(True, dict(OLD), REQUIRED_PAIRS, ())
    PARTIAL = IbkDbSnapshot(True, {"usd-krw": OLD["usd-krw"]}, ("usd-krw",), ("eur-krw", "jpy-krw"))

    def test_30_grant_only_preservation_carries_no_official_metadata(self):
        result = _build(db_after=self.KEPT,
                        preservation=IbkPreservationGrant(IbkReason.OFFICIAL_NO_SESSION),
                        intended=_retained_all(OLD))
        self.assertIs(result.status, IbkStatus.PRESERVED)
        self.assertIs(result.source, IbkSource.DB_SNAPSHOT)
        self.assertIsNone(result.observed_pairs)
        self.assertIsNone(result.observed_service_date)
        self.assertIsNone(result.official_completed_at)

    def test_31_generic_failures_are_degraded_or_failed(self):
        for reason in (IbkReason.AMBIGUOUS_TABLE_ABSENT, IbkReason.SELENIUM_STRICT_REJECTED):
            self.assertIs(_build(db_after=self.COMPLETE,
                                 failure=IbkObservationFailure(reason)).status, IbkStatus.DEGRADED)
        self.assertIs(_build(db_after=self.PARTIAL,
                             failure=IbkObservationFailure(IbkReason.TRANSPORT_ERROR)).status,
                      IbkStatus.FAILED)

    def test_32_unaccounted_pair_blocks_a_normal_verdict(self):
        state = IbkIntendedState(retained={"usd-krw": OLD["usd-krw"]},
                                 retention={"usd-krw": IbkRetentionReason.NOT_OBSERVED},
                                 unavailable=("eur-krw", "jpy-krw"))
        result = _build(db_after=self.PARTIAL,
                        preservation=IbkPreservationGrant(IbkReason.OFFICIAL_NO_SESSION),
                        intended=state)
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.OFFICIAL_NO_SESSION)

    def test_32b_unaccounted_pair_blocks_even_when_db_looks_complete(self):
        # complete 축과 accounted 축을 분리한다. DB에 값이 있어도, 무엇이어야 하는지
        # 설명하지 못한 통화가 있으면 정상 판정을 만들지 않는다.
        state = IbkIntendedState(retained={pair: OLD[pair] for pair in ("usd-krw", "jpy-krw")},
                                 retention={pair: IbkRetentionReason.NOT_OBSERVED
                                            for pair in ("usd-krw", "jpy-krw")},
                                 unavailable=("eur-krw",))
        grant = IbkPreservationGrant(IbkReason.OFFICIAL_NO_SESSION)
        settled = _build(db_after=self.KEPT, preservation=grant, intended=_retained_all(OLD))
        self.assertIs(settled.status, IbkStatus.PRESERVED)          # 대조군: 전부 설명함
        self.assertIs(self.KEPT.complete, True)
        result = _build(db_after=self.KEPT, preservation=grant, intended=state)
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.OFFICIAL_NO_SESSION)

    def test_33_other_service_date_is_never_promoted(self):
        with self.assertRaises(IbkResultBuildError):
            _build(db_after=self.COMPLETE, official=_observation(day=date(2026, 9, 4)),
                   intended=_submitted_all(), write=IbkWriteAttempt(True, IbkWriteMode.LEGACY, 3))

    def test_34_write_facts_must_match_the_intended_state(self):
        with self.assertRaises(IbkResultBuildError):  # 제출했다면 반환 개수가 있어야 한다
            _build(db_after=self.COMPLETE, official=_observation(), intended=_submitted_all(),
                   write=IbkWriteAttempt(True, IbkWriteMode.LEGACY, None))
        with self.assertRaises(IbkResultBuildError):  # 제출 의도와 쓰기 사실이 어긋난다
            _build(db_after=self.COMPLETE, official=_observation(), intended=_submitted_all(),
                   write=IbkWriteAttempt.none())
        with self.assertRaises(IbkResultBuildError):  # 제출값은 그 후보의 값이어야 한다
            _build(db_after=self.COMPLETE, official=_observation(),
                   intended=IbkIntendedState(submitted=dict(OLD)),
                   write=IbkWriteAttempt(True, IbkWriteMode.LEGACY, 3))

    def test_35_unknown_db_leaves_unknown_fields_unset(self):
        result = _build(db_after=IbkDbSnapshot.unknown(),
                        failure=IbkObservationFailure(IbkReason.TRANSPORT_ERROR))
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIsNone(result.preserved_pairs)
        self.assertIsNone(result.missing_pairs)
        self.assertIsNone(result.db_snapshot_complete)
        self.assertIsNone(result.changed_count)

    def test_36_result_passes_codec_and_parent_context_validation(self):
        result = _build(db_after=self.COMPLETE, official=_observation(), intended=_submitted_all(),
                        write=IbkWriteAttempt(True, IbkWriteMode.LEGACY, 3))
        decoded = decode_ibk_result(encode_ibk_result(result), RUN_ID)
        self.assertIs(decoded.status, IbkStatus.OBSERVED)
        context = IbkRunContext(RUN_ID, NOW - timedelta(seconds=1))
        self.assertEqual(context.expected_service_date, DAY.isoformat())
        context.validate_result(decoded, received_at=NOW + timedelta(seconds=1))
        with self.assertRaises(IbkProtocolError):
            IbkRunContext("b" * 32, NOW - timedelta(seconds=1)).validate_result(
                decoded, received_at=NOW + timedelta(seconds=1))

    def test_38_unavailable_boundary_is_uniform(self):
        # 같은 완전 DB·같은 보존 사유에서 설명 불가 통화 수만 바꾼다.
        grant = IbkPreservationGrant(IbkReason.OFFICIAL_NO_SESSION)
        for count in range(4):
            unavailable = tuple(sorted(REQUIRED_PAIRS[:count]))
            kept = {pair: OLD[pair] for pair in REQUIRED_PAIRS if pair not in unavailable}
            state = IbkIntendedState(retained=kept,
                                     retention={pair: IbkRetentionReason.NOT_OBSERVED for pair in kept},
                                     unavailable=unavailable)
            result = _build(db_after=self.KEPT, preservation=grant, intended=state)
            with self.subTest(unavailable=count):
                expected = IbkStatus.PRESERVED if count == 0 else IbkStatus.FAILED
                self.assertIs(result.status, expected)
                self.assertIs(result.reason, IbkReason.OFFICIAL_NO_SESSION)
                # 설명 불가는 'DB에 데이터가 없다'가 아니다. DB 사실은 그대로 남는다.
                self.assertIs(result.db_snapshot_complete, True)
                self.assertEqual(result.missing_pairs, ())

    def test_39_candidate_covering_all_pairs_rejects_unavailable(self):
        state = IbkIntendedState(retained={"usd-krw": OFFICIAL["usd-krw"]},
                                 retention={"usd-krw": IbkRetentionReason.REGRESSION_GUARD},
                                 unavailable=("eur-krw", "jpy-krw"))
        with self.assertRaises(IbkResultBuildError):
            _build(db_after=self.COMPLETE, official=_observation(), intended=state,
                   write=IbkWriteAttempt.none())

    def test_40_mismatch_cause_is_separated_from_the_write_gate(self):
        kept = {"jpy-krw": OLD["jpy-krw"], "eur-krw": OLD["eur-krw"]}
        guard = {pair: IbkRetentionReason.REGRESSION_GUARD for pair in kept}
        candidate = IbkOfficialObservation(IbkSource.OFFICIAL_POST, DAY, dict(OFFICIAL), COMPLETED)
        state = IbkIntendedState(submitted={"usd-krw": OFFICIAL["usd-krw"]},
                                 retained=kept, retention=guard)
        settled = IbkDbSnapshot(True, {"usd-krw": OFFICIAL["usd-krw"], **kept}, REQUIRED_PAIRS, ())
        drifted = IbkDbSnapshot(True, {**settled.rates, "jpy-krw": 935.0}, REQUIRED_PAIRS, ())
        unapplied = IbkDbSnapshot(True, {"usd-krw": OLD["usd-krw"], **kept}, REQUIRED_PAIRS, ())
        both = IbkDbSnapshot(True, {"usd-krw": OLD["usd-krw"], **kept, "jpy-krw": 935.0},
                             REQUIRED_PAIRS, ())
        for mode in (IbkWriteMode.BLOCKED_UNINITIALIZED, IbkWriteMode.LEGACY):
            write = IbkWriteAttempt(True, mode, 0)
            with self.subTest(mode=mode.value):
                control = _build(db_after=settled, official=candidate, intended=state, write=write)
                self.assertIs(control.reason, IbkReason.REGRESSION_GUARD)  # 대조군: 둘 다 일치
                # 보존값 변경은 차단으로 설명되지 않는다.
                self.assertIs(_build(db_after=drifted, official=candidate, intended=state,
                                     write=write).reason, IbkReason.DB_APPLY_MISMATCH)
                # 둘 다 어긋나면 차단 하나로 전체 원인을 설명하지 않는다.
                self.assertIs(_build(db_after=both, official=candidate, intended=state,
                                     write=write).reason, IbkReason.DB_APPLY_MISMATCH)
                only_submitted = _build(db_after=unapplied, official=candidate, intended=state,
                                        write=write).reason
                self.assertIs(only_submitted,
                              IbkReason.WRITE_POLICY_BLOCKED if mode.blocked
                              else IbkReason.DB_APPLY_MISMATCH)

    def test_37_input_facts_reject_misuse(self):
        with self.assertRaises(IbkResultBuildError):
            IbkPreservationGrant(IbkReason.TRANSPORT_ERROR)
        with self.assertRaises(IbkResultBuildError):
            IbkObservationFailure(IbkReason.NORMAL)
        with self.assertRaises(IbkResultBuildError):
            IbkObservationFailure(IbkReason.PREOPEN_PENDING)
        with self.assertRaises(IbkResultBuildError):
            IbkOfficialObservation(IbkSource.DB_SNAPSHOT, DAY, dict(OFFICIAL), COMPLETED)
        with self.assertRaises(IbkResultBuildError):
            IbkOfficialObservation(IbkSource.OFFICIAL_GET, DAY, dict(OFFICIAL),
                                   datetime(2026, 9, 7, 8, 30, 35))
        with self.assertRaises(IbkResultBuildError):
            IbkWriteAttempt(True, None, 3)
        with self.assertRaises(IbkResultBuildError):  # 3통화를 다 설명하지 않았다
            IbkIntendedState(submitted={"usd-krw": OFFICIAL["usd-krw"]})
        with self.assertRaises(IbkResultBuildError):  # 제외 사유 없음
            IbkIntendedState(submitted={"usd-krw": OFFICIAL["usd-krw"]},
                             retained={pair: OLD[pair] for pair in ("jpy-krw", "eur-krw")})
        with self.assertRaises(IbkResultBuildError):  # 겹침
            IbkIntendedState(submitted=dict(OFFICIAL), retained={"usd-krw": OLD["usd-krw"]},
                             retention={"usd-krw": IbkRetentionReason.REGRESSION_GUARD})
        with self.assertRaises(IbkResultBuildError):
            _build(db_after=self.COMPLETE)
        with self.assertRaises(IbkResultBuildError):
            _build(db_after=self.COMPLETE, official=_observation(), intended=_submitted_all(),
                   failure=IbkObservationFailure(IbkReason.DB_ERROR))
        with self.assertRaises(IbkResultBuildError):  # 실패 사실에는 의도한 상태가 없다
            _build(db_after=self.COMPLETE, failure=IbkObservationFailure(IbkReason.DB_ERROR),
                   intended=_submitted_all())
        with self.assertRaises(IbkResultBuildError):  # 관측·보존 경로엔 의도한 상태가 필요하다
            _build(db_after=self.COMPLETE, preservation=IbkPreservationGrant(IbkReason.PREOPEN_PENDING))
        with self.assertRaises(IbkResultBuildError):  # 당일 관측은 보존 상황이 아니다
            _build(db_after=self.COMPLETE, official=_observation(),
                   preservation=IbkPreservationGrant(IbkReason.PREOPEN_PENDING),
                   intended=_submitted_all(), write=IbkWriteAttempt(True, IbkWriteMode.LEGACY, 0))
        with self.assertRaises(IbkResultBuildError):  # 후보가 없는데 회귀 가드 사유
            _build(db_after=self.KEPT, preservation=IbkPreservationGrant(IbkReason.PREOPEN_PENDING),
                   intended=IbkIntendedState(retained=dict(OLD),
                                             retention={pair: IbkRetentionReason.REGRESSION_GUARD
                                                        for pair in OLD}))
        with self.assertRaises(IbkResultBuildError):
            _build(db_after=self.COMPLETE, observed_at=datetime(2026, 9, 7, 8, 30, 40),
                   failure=IbkObservationFailure(IbkReason.DB_ERROR))
