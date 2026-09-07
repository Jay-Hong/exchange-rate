"""IBK 회귀 가드 추출 — 순수 함수 규칙, 원본 동결본 대조, 호출부 행동 보존.

추출은 규칙을 바꾸지 않는다. 경계와 같으면 회귀가 아니고(초과일 때만), 같은 값 판정이
timestamp 처리보다 앞선다. 이 파일은 그 두 성질과 호출부 계약을 함께 잠근다.
"""

import copy
import datetime
import unittest
from unittest.mock import MagicMock, patch

from app.crawlers import ibk
from app.ibk_regression_guard import find_regressing_pairs

KST = ibk.KST
PAIRS = ("usd-krw", "jpy-krw", "eur-krw")
RATES = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
NEWER = {"usd-krw": 1383.0, "jpy-krw": 868.0, "eur-krw": 1611.0}
COMPLETION = KST.localize(datetime.datetime(2026, 8, 27, 5, 59, 55))
TOLERANCE = ibk.IBK_DB_SAVE_LAG_TOLERANCE_SECONDS


def _naive_utc(aware):
    return aware.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _info(rates, timestamp):
    return {pair: {"rate": rates.get(pair), "timestamp": timestamp} for pair in PAIRS}


def _original_loop(current_rates, last_info, candidate_completion_kst, tolerance):
    """추출 직전 ibk.py 루프의 동결 사본. 규칙 변화를 대조하기 위한 기준이다."""
    regressing_pairs = []
    for pair, info in last_info.items():
        last_rate = info.get("rate")
        if last_rate is None or current_rates.get(pair) == last_rate:
            continue
        last_timestamp = info.get("timestamp")
        if last_timestamp is None:
            regressing_pairs.append(pair)
            continue
        if last_timestamp.tzinfo is None:
            last_timestamp = last_timestamp.replace(tzinfo=datetime.timezone.utc)
        last_timestamp_kst = last_timestamp.astimezone(KST)
        if last_timestamp_kst > candidate_completion_kst + datetime.timedelta(seconds=tolerance):
            regressing_pairs.append(pair)
    return regressing_pairs


class RegressionGuardRulesTest(unittest.TestCase):
    def test_empty_last_info_excludes_nothing(self):
        self.assertEqual(find_regressing_pairs(RATES, {}, COMPLETION, TOLERANCE), [])

    def test_same_value_or_absent_db_value_is_not_excluded(self):
        later = _naive_utc(COMPLETION + datetime.timedelta(hours=1))
        same = _info(RATES, later)                       # 값이 같으면 시각과 무관
        self.assertEqual(find_regressing_pairs(RATES, same, COMPLETION, TOLERANCE), [])
        absent = {pair: {"rate": None, "timestamp": later} for pair in PAIRS}
        self.assertEqual(find_regressing_pairs(RATES, absent, COMPLETION, TOLERANCE), [])
        # 대조군: 같은 시각이라도 값이 다르면 전부 제외된다.
        self.assertEqual(find_regressing_pairs(RATES, _info(NEWER, later), COMPLETION, TOLERANCE),
                         list(PAIRS))

    def test_missing_timestamp_with_different_value_is_excluded(self):
        no_stamp = {pair: {"rate": NEWER[pair], "timestamp": None} for pair in PAIRS}
        self.assertEqual(find_regressing_pairs(RATES, no_stamp, COMPLETION, TOLERANCE), list(PAIRS))

    def test_naive_utc_and_aware_same_instant_agree(self):
        for offset in (-1, 0, 1):
            moment = COMPLETION + datetime.timedelta(seconds=TOLERANCE + offset)
            naive = find_regressing_pairs(RATES, _info(NEWER, _naive_utc(moment)),
                                          COMPLETION, TOLERANCE)
            aware = find_regressing_pairs(RATES, _info(NEWER, moment), COMPLETION, TOLERANCE)
            utc = find_regressing_pairs(RATES, _info(NEWER, moment.astimezone(datetime.timezone.utc)),
                                        COMPLETION, TOLERANCE)
            with self.subTest(offset=offset):
                self.assertEqual(naive, aware)
                self.assertEqual(naive, utc)

    def test_tolerance_boundary_is_strictly_greater(self):
        for offset, expected in ((-1, []), (0, []), (1, list(PAIRS))):
            moment = COMPLETION + datetime.timedelta(seconds=TOLERANCE + offset)
            with self.subTest(offset=offset):
                self.assertEqual(
                    find_regressing_pairs(RATES, _info(NEWER, _naive_utc(moment)),
                                          COMPLETION, TOLERANCE),
                    expected,
                )

    def test_partial_and_full_exclusion_preserve_last_info_order(self):
        old = _naive_utc(COMPLETION)
        new = _naive_utc(COMPLETION + datetime.timedelta(seconds=TOLERANCE + 1))
        mixed = {
            "usd-krw": {"rate": NEWER["usd-krw"], "timestamp": new},
            "jpy-krw": {"rate": NEWER["jpy-krw"], "timestamp": old},
            "eur-krw": {"rate": NEWER["eur-krw"], "timestamp": new},
        }
        self.assertEqual(find_regressing_pairs(RATES, mixed, COMPLETION, TOLERANCE),
                         ["usd-krw", "eur-krw"])          # 정렬하지 않고 순회 순서를 따른다
        self.assertEqual(find_regressing_pairs(RATES, _info(NEWER, new), COMPLETION, TOLERANCE),
                         list(PAIRS))

    def test_inputs_are_not_modified(self):
        stamp = _naive_utc(COMPLETION + datetime.timedelta(seconds=TOLERANCE + 1))
        current, last = dict(RATES), _info(NEWER, stamp)
        before_current, before_last = copy.deepcopy(current), copy.deepcopy(last)
        find_regressing_pairs(current, last, COMPLETION, TOLERANCE)
        self.assertEqual(current, before_current)
        self.assertEqual(last, before_last)
        self.assertIsNone(last["usd-krw"]["timestamp"].tzinfo)   # naive 그대로 남는다


class FrozenOriginalEquivalenceTest(unittest.TestCase):
    """추출본이 동결한 원본 루프와 같은 답을 내는지 행렬로 대조한다."""

    def test_matches_frozen_original_across_matrix(self):
        stamps = [None,
                  _naive_utc(COMPLETION - datetime.timedelta(seconds=1)),
                  _naive_utc(COMPLETION + datetime.timedelta(seconds=TOLERANCE)),
                  _naive_utc(COMPLETION + datetime.timedelta(seconds=TOLERANCE + 1)),
                  COMPLETION + datetime.timedelta(seconds=TOLERANCE),
                  (COMPLETION + datetime.timedelta(seconds=TOLERANCE + 1)).astimezone(
                      datetime.timezone.utc)]
        values = [None, RATES["usd-krw"], NEWER["usd-krw"]]
        checked = 0
        for stamp in stamps:
            for value in values:
                last = {"usd-krw": {"rate": value, "timestamp": stamp},
                        "jpy-krw": {"rate": NEWER["jpy-krw"], "timestamp": stamp},
                        "eur-krw": {"rate": None, "timestamp": None}}
                with self.subTest(stamp=str(stamp), value=value):
                    self.assertEqual(
                        find_regressing_pairs(RATES, last, COMPLETION, TOLERANCE),
                        _original_loop(RATES, last, COMPLETION, TOLERANCE),
                    )
                checked += 1
        self.assertEqual(checked, 18)   # 행렬이 실제로 돌았는지 확인


class CallSiteBehaviorTest(unittest.TestCase):
    """추출 후에도 호출부의 조회·저장 인자와 outcome이 그대로인지 확인한다."""

    def _run(self, last_info, insert_return=1):
        db = MagicMock()
        reference_time = KST.localize(datetime.datetime(2026, 8, 27, 10, 0))
        with patch.object(ibk, "_fetch_ibk_rates_for_date",
                          side_effect=[None, (dict(RATES), "05:59:55")]), \
             patch.object(ibk.crud, "get_last_bank_rates_with_ts",
                          return_value=last_info) as mock_query, \
             patch.object(ibk.crud, "insert_bank_rates_into_db",
                          return_value=insert_return) as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)
        return outcome, db, mock_query, mock_insert

    def test_partial_regression_still_stores_remaining_pairs(self):
        new = _naive_utc(COMPLETION + datetime.timedelta(days=1))
        last = {"usd-krw": {"rate": NEWER["usd-krw"], "timestamp": new},
                "jpy-krw": {"rate": NEWER["jpy-krw"], "timestamp": new},
                "eur-krw": {"rate": None, "timestamp": None}}
        outcome, db, mock_query, mock_insert = self._run(last)
        self.assertIs(outcome, ibk.DatedRequestOutcome.OBSERVED)
        mock_query.assert_called_once_with(db, ibk.BANK_NAME, ibk.MIBANK_REQUIRED_PAIRS)
        mock_insert.assert_called_once_with(db=db, current_rates={"eur-krw": RATES["eur-krw"]},
                                            bank_name=ibk.BANK_NAME)

    def test_full_regression_blocks_write_and_preserves(self):
        new = _naive_utc(COMPLETION + datetime.timedelta(days=1))
        outcome, db, mock_query, mock_insert = self._run(_info(NEWER, new))
        self.assertIs(outcome, ibk.DatedRequestOutcome.PRESERVED)
        mock_query.assert_called_once_with(db, ibk.BANK_NAME, ibk.MIBANK_REQUIRED_PAIRS)
        mock_insert.assert_not_called()

    def test_no_regression_stores_every_pair(self):
        old = _naive_utc(COMPLETION)
        outcome, db, _, mock_insert = self._run(_info(NEWER, old))
        self.assertIs(outcome, ibk.DatedRequestOutcome.OBSERVED)
        mock_insert.assert_called_once_with(db=db, current_rates=dict(RATES),
                                            bank_name=ibk.BANK_NAME)
