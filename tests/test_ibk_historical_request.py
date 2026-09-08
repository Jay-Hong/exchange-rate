"""IBK 공식 날짜 지정 Request fast path 회귀 테스트."""

import datetime
import unittest
from unittest.mock import MagicMock, patch

from app.crawlers import ibk


_DEFAULT_RATES = {
    "USD": "1,382.00",
    "JPY": "867.06",
    "EUR": "1,610.31",
}
_HISTORICAL_REFERENCE = ibk.KST.localize(datetime.datetime(2026, 8, 28, 6, 5))


def _utc_naive(kst_datetime):
    return kst_datetime.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _last_info(rates, timestamp):
    return {
        pair: {"rate": rate, "timestamp": timestamp}
        for pair, rate in rates.items()
    }


def _ibk_html(
    *,
    selected_date="2026.08.27",
    rates=None,
    order=("USD", "JPY", "EUR"),
    caption="일반고시환율 표",
    rate_header="매매기준율",
    completed_at="05:59:55",
):
    rates = _DEFAULT_RATES if rates is None else rates
    rows = "".join(
        "<tr>"
        f"<th scope='row'>{code}</th><th>{code} name</th>"
        f"<td>{rates.get(code, '-')}</td><td>0</td>"
        "</tr>"
        for code in order
    )
    standard = (
        f"<p class='standard'>고시완료 시각 : {completed_at}</p>"
        if completed_at is not None else ""
    )
    return f"""
        <html><body>
          <input id="inDate" value="{selected_date}">
          {standard}
          <table>
            <caption>{caption}</caption>
            <thead><tr><th>통화</th><th>통화명</th><th>{rate_header}</th><th>기타</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </body></html>
    """


def _no_notice_html(*, selected_date="2026.08.17"):
    return f"""
        <html><body>
          <input id="inDate" value="{selected_date}">
          <div>ECBKFEX01589 조회된 데이터가 없습니다.</div>
        </body></html>
    """


def _table_absent_html(*, selected_date="2026.08.27", body=""):
    return f"""
        <html><body>
          <input id="inDate" value="{selected_date}">
          {body}
        </body></html>
    """


def _response(html):
    response = MagicMock()
    response.text = html
    response.raise_for_status.return_value = None
    return response


class TestIbkDatedRequestParser(unittest.TestCase):
    def test_completion_clock_maps_preopen_tail_to_next_calendar_day(self):
        service_date = datetime.date(2026, 8, 27)

        self.assertEqual(
            ibk._ibk_completion_kst(service_date, "07:59:59"),
            ibk.KST.localize(datetime.datetime(2026, 8, 28, 7, 59, 59)),
        )
        self.assertEqual(
            ibk._ibk_completion_kst(service_date, "08:00:00"),
            ibk.KST.localize(datetime.datetime(2026, 8, 27, 8, 0, 0)),
        )

    def test_valid_response_uses_official_post_contract(self):
        query_date = datetime.date(2026, 8, 27)
        with patch.object(ibk.requests, "post", return_value=_response(_ibk_html())) as mock_post:
            rates, completed_at = ibk._fetch_ibk_rates_for_date(
                query_date,
                reference_time=_HISTORICAL_REFERENCE,
            )

        self.assertEqual(
            rates,
            {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31},
        )
        self.assertEqual(completed_at, "05:59:55")
        mock_post.assert_called_once_with(
            ibk.IBK_BANK_URL,
            data={
                "pageId": ibk.IBK_PAGE_ID,
                "dsCd": "",
                "curCd": "",
                "ecrtInqyDscd": "01",
                "efpsId": "",
                "inDate": "2026.08.27",
            },
            headers=ibk.HEADERS,
            timeout=ibk.DEFAULT_TIMEOUT,
        )

    def test_currency_rows_can_move_and_extra_rows_are_ignored(self):
        html = _ibk_html(order=("CNY", "EUR", "USD", "JPY"))
        with patch.object(ibk.requests, "post", return_value=_response(html)):
            rates, _ = ibk._fetch_ibk_rates_for_date(
                datetime.date(2026, 8, 27),
                reference_time=_HISTORICAL_REFERENCE,
            )
        self.assertEqual(set(rates), {"usd-krw", "jpy-krw", "eur-krw"})

    def test_exact_no_notice_response_returns_none(self):
        html = _no_notice_html()
        with patch.object(ibk.requests, "post", return_value=_response(html)):
            result = ibk._fetch_ibk_rates_for_date(
                datetime.date(2026, 8, 17),
                reference_time=_HISTORICAL_REFERENCE,
            )
        self.assertIsNone(result)

    def test_matching_date_without_table_raises_distinct_absent_state(self):
        html = _table_absent_html()
        with patch.object(ibk.requests, "post", return_value=_response(html)):
            with self.assertRaisesRegex(
                ibk.IbkRateTableAbsentError,
                "일반고시환율 표 누락",
            ):
                ibk._fetch_ibk_rates_for_date(
                    datetime.date(2026, 8, 27),
                    reference_time=ibk.KST.localize(
                        datetime.datetime(2026, 8, 27, 8, 10)
                    ),
                )

    def test_wrong_server_readback_is_rejected(self):
        html = _ibk_html(selected_date="2026.08.28")
        with patch.object(ibk.requests, "post", return_value=_response(html)):
            with self.assertRaisesRegex(ValueError, "readback"):
                ibk._fetch_ibk_rates_for_date(
                    datetime.date(2026, 8, 27),
                    reference_time=_HISTORICAL_REFERENCE,
                )

    def test_partial_currency_set_is_rejected(self):
        html = _ibk_html(rates={"USD": "1,382.00", "JPY": "867.06"})
        with patch.object(ibk.requests, "post", return_value=_response(html)):
            with self.assertRaisesRegex(ValueError, "필수 통화 누락"):
                ibk._fetch_ibk_rates_for_date(
                    datetime.date(2026, 8, 27),
                    reference_time=_HISTORICAL_REFERENCE,
                )

    def test_non_finite_and_out_of_range_rates_are_rejected(self):
        for bad_rate, message in (("NaN", "유한하지 않은"), ("9,999.00", "범위 초과")):
            with self.subTest(rate=bad_rate):
                html = _ibk_html(rates={**_DEFAULT_RATES, "USD": bad_rate})
                with patch.object(ibk.requests, "post", return_value=_response(html)):
                    with self.assertRaisesRegex(ValueError, message):
                        ibk._fetch_ibk_rates_for_date(
                            datetime.date(2026, 8, 27),
                            reference_time=_HISTORICAL_REFERENCE,
                        )

    def test_header_and_completed_time_are_required(self):
        cases = (
            (_ibk_html(rate_header="기준율"), "매매기준율 헤더 누락"),
            (_ibk_html(completed_at=None), "고시완료 시각 누락"),
        )
        for html, message in cases:
            with self.subTest(message=message), \
                 patch.object(ibk.requests, "post", return_value=_response(html)):
                with self.assertRaisesRegex(ValueError, message):
                    ibk._fetch_ibk_rates_for_date(
                        datetime.date(2026, 8, 27),
                        reference_time=_HISTORICAL_REFERENCE,
                    )

    def test_invalid_or_future_completed_time_is_rejected(self):
        cases = (
            ("99:99:99", _HISTORICAL_REFERENCE, "형식 오류"),
            (
                "05:59:55",
                ibk.KST.localize(datetime.datetime(2026, 8, 28, 3, 0)),
                "조회 시각보다 미래",
            ),
        )
        for completed_at, reference_time, message in cases:
            with self.subTest(completed_at=completed_at), patch.object(
                ibk.requests,
                "post",
                return_value=_response(_ibk_html(completed_at=completed_at)),
            ):
                with self.assertRaisesRegex(ValueError, message):
                    ibk._fetch_ibk_rates_for_date(
                        datetime.date(2026, 8, 27),
                        reference_time=reference_time,
                    )

    def test_current_get_uses_same_strict_three_currency_contract(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 27, 10, 0))
        partial = _ibk_html(rates={"USD": "1,382.00"}, completed_at="09:59:55")
        with patch.object(ibk.requests, "get", return_value=_response(partial)), patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
        ) as mock_insert:
            ok = ibk.try_crawl_with_requests(db, reference_time=reference_time)

        self.assertFalse(ok)
        mock_insert.assert_not_called()

    def test_current_get_success_stores_ibk_rates_and_returns_true(self):
        """당일 GET 성공 경로: 실제 bank_name='ibk' 저장 + True 반환까지 잠근다.

        거부 경로만 검증하면 은행명 오기입·insert 누락·반환값 반전이
        모두 통과한다(change-only 저장이라 '0행'이 '무고시'와 구분되지 않아
        손실이 조용하다).
        """
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 27, 10, 0))
        html = _ibk_html(selected_date="2026.08.27", completed_at="09:59:55")
        with patch.object(ibk.requests, "get", return_value=_response(html)), patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
        ) as mock_insert:
            ok = ibk.try_crawl_with_requests(db, reference_time=reference_time)

        self.assertTrue(ok)
        mock_insert.assert_called_once_with(
            db=db,
            current_rates={
                "usd-krw": 1382.0,
                "jpy-krw": 867.06,
                "eur-krw": 1610.31,
            },
            bank_name="ibk",
        )


class TestIbkDatedLookback(unittest.TestCase):
    def test_current_preopen_table_absence_with_equal_prior_snapshot_is_noop(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 27, 8, 10))
        rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        prior_completed = ibk.KST.localize(datetime.datetime(2026, 8, 27, 5, 59, 55))
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            side_effect=[
                ibk.IbkRateTableAbsentError("IBK 일반고시환율 표 누락"),
                (rates, "05:59:55"),
            ],
        ) as mock_fetch, patch.object(
            ibk.crud,
            "get_last_bank_rates_with_ts",
            return_value=_last_info(rates, _utc_naive(prior_completed)),
        ), patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
            return_value=0,
        ) as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.OBSERVED)
        self.assertEqual(
            [item.args[0] for item in mock_fetch.call_args_list],
            [datetime.date(2026, 8, 27), datetime.date(2026, 8, 26)],
        )
        mock_insert.assert_called_once_with(
            db=db,
            current_rates=rates,
            bank_name=ibk.BANK_NAME,
        )

    def test_current_preopen_table_absence_blocks_regressing_prior_snapshot(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 27, 8, 10))
        prior_rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        current_rates = {"usd-krw": 1383.0, "jpy-krw": 868.0, "eur-krw": 1611.0}
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            side_effect=[
                ibk.IbkRateTableAbsentError("IBK 일반고시환율 표 누락"),
                (prior_rates, "05:59:55"),
            ],
        ), patch.object(
            ibk.crud,
            "get_last_bank_rates_with_ts",
            return_value=_last_info(
                current_rates,
                _utc_naive(ibk.KST.localize(datetime.datetime(2026, 8, 27, 7, 0))),
            ),
        ), patch.object(ibk.crud, "insert_bank_rates_into_db") as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.PRESERVED)
        mock_insert.assert_not_called()

    def test_current_preopen_table_absence_catches_up_newer_prior_completion(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 27, 8, 10))
        intermediate_rates = {
            "usd-krw": 1381.0,
            "jpy-krw": 866.0,
            "eur-krw": 1609.0,
        }
        final_rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            side_effect=[
                ibk.IbkRateTableAbsentError("IBK 일반고시환율 표 누락"),
                (final_rates, "05:59:55"),
            ],
        ), patch.object(
            ibk.crud,
            "get_last_bank_rates_with_ts",
            return_value=_last_info(
                intermediate_rates,
                _utc_naive(ibk.KST.localize(datetime.datetime(2026, 8, 27, 5, 0))),
            ),
        ), patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
            return_value=3,
        ) as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.OBSERVED)
        mock_insert.assert_called_once_with(
            db=db,
            current_rates=final_rates,
            bank_name=ibk.BANK_NAME,
        )

    def test_preopen_table_absence_preserves_holiday_and_weekend_walkback(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 18, 8, 10))
        friday_rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}

        def result_for_date(query_date, **_):
            if query_date == datetime.date(2026, 8, 18):
                raise ibk.IbkRateTableAbsentError("IBK 일반고시환율 표 누락")
            if query_date == datetime.date(2026, 8, 17):
                return None
            if query_date == datetime.date(2026, 8, 14):
                return friday_rates, "05:59:51"
            raise AssertionError(f"unexpected date: {query_date}")

        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            side_effect=result_for_date,
        ) as mock_fetch, patch.object(
            ibk.crud,
            "get_last_bank_rates_with_ts",
            return_value=_last_info(
                friday_rates,
                _utc_naive(ibk.KST.localize(datetime.datetime(2026, 8, 15, 5, 59, 51))),
            ),
        ), patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
            return_value=0,
        ):
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.OBSERVED)
        self.assertEqual(
            [item.args[0] for item in mock_fetch.call_args_list],
            [
                datetime.date(2026, 8, 18),
                datetime.date(2026, 8, 17),
                datetime.date(2026, 8, 14),
            ],
        )

    def test_table_absence_only_walks_back_for_current_date_in_preopen_window(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 27, 8, 10))
        absent = ibk.IbkRateTableAbsentError("IBK 일반고시환율 표 누락")
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            side_effect=[absent, absent],
        ) as mock_fetch, patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
        ) as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.FALLBACK)
        self.assertEqual(
            [item.args[0] for item in mock_fetch.call_args_list],
            [datetime.date(2026, 8, 27), datetime.date(2026, 8, 26)],
        )
        mock_insert.assert_not_called()

    def test_preopen_table_absence_window_has_inclusive_start_and_exclusive_end(self):
        rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        allowed = (
            datetime.datetime(2026, 8, 27, 8, 0, 0),
            datetime.datetime(2026, 8, 27, 8, 34, 59),
        )
        for naive_reference in allowed:
            with self.subTest(reference_time=naive_reference):
                db = MagicMock()
                reference_time = ibk.KST.localize(naive_reference)
                with patch.object(
                    ibk,
                    "_fetch_ibk_rates_for_date",
                    side_effect=[
                        ibk.IbkRateTableAbsentError("IBK 일반고시환율 표 누락"),
                        (rates, "05:59:55"),
                    ],
                ) as mock_fetch, patch.object(
                    ibk.crud,
                    "get_last_bank_rates_with_ts",
                    return_value=_last_info(
                        rates,
                        _utc_naive(
                            ibk.KST.localize(datetime.datetime(2026, 8, 27, 5, 59, 55))
                        ),
                    ),
                ), patch.object(
                    ibk.crud,
                    "insert_bank_rates_into_db",
                    return_value=0,
                ):
                    outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

                self.assertIs(outcome, ibk.DatedRequestOutcome.OBSERVED)
                self.assertEqual(mock_fetch.call_count, 2)

        rejected = (
            datetime.datetime(2026, 8, 27, 7, 59, 59),
            datetime.datetime(2026, 8, 27, 8, 35, 0),
        )
        for naive_reference in rejected:
            with self.subTest(reference_time=naive_reference):
                db = MagicMock()
                reference_time = ibk.KST.localize(naive_reference)
                with patch.object(
                    ibk,
                    "_fetch_ibk_rates_for_date",
                    side_effect=ibk.IbkRateTableAbsentError(
                        "IBK 일반고시환율 표 누락"
                    ),
                ) as mock_fetch:
                    outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

                self.assertIs(outcome, ibk.DatedRequestOutcome.FALLBACK)
                self.assertEqual(mock_fetch.call_count, 1)

    def test_wrong_readback_error_inside_preopen_window_falls_back(self):
        db = MagicMock()
        malformed = _table_absent_html(
            selected_date="2026.08.26",
            body="<div>일시적인 오류가 발생했습니다.</div>",
        )
        with patch.object(
            ibk.requests,
            "post",
            return_value=_response(malformed),
        ) as mock_post, patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
        ) as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(
                db,
                ibk.KST.localize(datetime.datetime(2026, 8, 27, 8, 10)),
            )

        self.assertIs(outcome, ibk.DatedRequestOutcome.FALLBACK)
        self.assertEqual(mock_post.call_count, 1)
        mock_insert.assert_not_called()

    def test_explicit_error_message_inside_preopen_window_does_not_walk_back(self):
        db = MagicMock()
        error_page = _table_absent_html(
            body="<div>일시적인 오류가 발생했습니다.</div>",
        )
        prior_page = _ibk_html(
            selected_date="2026.08.26",
            completed_at="05:59:55",
        )
        with patch.object(
            ibk.requests,
            "post",
            side_effect=[_response(error_page), _response(prior_page)],
        ) as mock_post, patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
        ) as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(
                db,
                ibk.KST.localize(datetime.datetime(2026, 8, 27, 8, 10)),
            )

        self.assertIs(outcome, ibk.DatedRequestOutcome.FALLBACK)
        self.assertEqual(mock_post.call_count, 1)
        mock_insert.assert_not_called()

    def test_transport_error_inside_preopen_window_falls_back_immediately(self):
        db = MagicMock()
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            side_effect=ibk.requests.Timeout("timed out"),
        ) as mock_fetch, patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
        ) as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(
                db,
                ibk.KST.localize(datetime.datetime(2026, 8, 27, 8, 10)),
            )

        self.assertIs(outcome, ibk.DatedRequestOutcome.FALLBACK)
        self.assertEqual(mock_fetch.call_count, 1)
        mock_insert.assert_not_called()

    def test_skips_weekends_and_continues_only_after_explicit_no_notice(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 18, 7, 50))
        rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}

        def result_for_date(query_date, **_):
            if query_date == datetime.date(2026, 8, 17):
                return None
            if query_date == datetime.date(2026, 8, 14):
                return rates, "23:59:51"
            raise AssertionError(f"unexpected date: {query_date}")

        empty_last = {
            pair: {"rate": None, "timestamp": None}
            for pair in ibk.MIBANK_REQUIRED_PAIRS
        }
        with patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=result_for_date) as mock_fetch, \
             patch.object(ibk.crud, "get_last_bank_rates_with_ts", return_value=empty_last), \
             patch.object(ibk.crud, "insert_bank_rates_into_db", return_value=0) as mock_insert:
            ok = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(ok, ibk.DatedRequestOutcome.OBSERVED)
        self.assertEqual(
            [item.args[0] for item in mock_fetch.call_args_list],
            [datetime.date(2026, 8, 17), datetime.date(2026, 8, 14)],
        )
        mock_insert.assert_called_once_with(
            db=db,
            current_rates=rates,
            bank_name=ibk.BANK_NAME,
        )

    def test_preserves_complete_database_after_bounded_no_notice_lookback(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 18, 7, 50))
        existing_rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        with patch.object(ibk, "_fetch_ibk_rates_for_date", return_value=None) as mock_fetch, \
             patch.object(
                 ibk.crud,
                 "get_last_bank_rates_with_ts",
                 return_value=_last_info(
                     existing_rates,
                     _utc_naive(ibk.KST.localize(datetime.datetime(2026, 8, 14, 23, 59, 51))),
                 ),
             ), \
             patch.object(ibk.crud, "insert_bank_rates_into_db") as mock_insert:
            ok = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(ok, ibk.DatedRequestOutcome.PRESERVED)
        queried = [item.args[0] for item in mock_fetch.call_args_list]
        self.assertEqual(
            queried,
            [
                datetime.date(2026, 8, 17),
                datetime.date(2026, 8, 14),
                datetime.date(2026, 8, 13),
                datetime.date(2026, 8, 12),
                datetime.date(2026, 8, 11),
                datetime.date(2026, 8, 10),
            ],
        )
        mock_insert.assert_not_called()

    def test_all_no_notice_falls_back_when_database_is_empty(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 18, 7, 50))
        empty_last = {
            pair: {"rate": None, "timestamp": None}
            for pair in ibk.MIBANK_REQUIRED_PAIRS
        }
        with patch.object(ibk, "_fetch_ibk_rates_for_date", return_value=None), patch.object(
            ibk.crud,
            "get_last_bank_rates_with_ts",
            return_value=empty_last,
        ), patch.object(ibk.crud, "insert_bank_rates_into_db") as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.FALLBACK)
        mock_insert.assert_not_called()

    def test_all_no_notice_falls_back_when_database_is_partial(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 18, 7, 50))
        saved_at = _utc_naive(
            ibk.KST.localize(datetime.datetime(2026, 8, 14, 23, 59, 51))
        )
        partial_last = _last_info(
            {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31},
            saved_at,
        )
        partial_last["eur-krw"]["timestamp"] = None
        with patch.object(ibk, "_fetch_ibk_rates_for_date", return_value=None), patch.object(
            ibk.crud,
            "get_last_bank_rates_with_ts",
            return_value=partial_last,
        ):
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.FALLBACK)

    def test_transport_or_contract_error_stops_lookback_immediately(self):
        db = MagicMock()
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            side_effect=ValueError("WAF or malformed response"),
        ) as mock_fetch:
            ok = ibk.try_crawl_with_dated_requests(
                db,
                ibk.KST.localize(datetime.datetime(2026, 8, 18, 7, 50)),
            )

        self.assertIs(ok, ibk.DatedRequestOutcome.FALLBACK)
        self.assertEqual(mock_fetch.call_args.args[0], datetime.date(2026, 8, 17))

    def test_after_market_open_retries_today_before_looking_back(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 27, 10, 0))
        rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            return_value=(rates, "10:00:01"),
        ) as mock_fetch, patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
            return_value=0,
        ):
            ok = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(ok, ibk.DatedRequestOutcome.OBSERVED)
        self.assertEqual(mock_fetch.call_args.args[0], datetime.date(2026, 8, 27))

    def test_exact_service_date_rollover_queries_today(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 27, 8, 0))
        rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            return_value=(rates, "08:00:00"),
        ) as mock_fetch, patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
            return_value=0,
        ):
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.OBSERVED)
        self.assertEqual(mock_fetch.call_args.args[0], datetime.date(2026, 8, 27))

    def test_after_open_does_not_replace_current_db_value_with_older_service_date(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 27, 10, 0))
        old_rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        current_rates = {"usd-krw": 1383.0, "jpy-krw": 868.0, "eur-krw": 1611.0}
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            side_effect=[None, (old_rates, "05:59:55")],
        ) as mock_fetch, patch.object(
            ibk.crud,
            "get_last_bank_rates_with_ts",
            return_value=_last_info(
                current_rates,
                _utc_naive(ibk.KST.localize(datetime.datetime(2026, 8, 27, 9, 0))),
            ),
        ), patch.object(ibk.crud, "insert_bank_rates_into_db") as mock_insert:
            ok = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(ok, ibk.DatedRequestOutcome.PRESERVED)
        self.assertEqual(
            [item.args[0] for item in mock_fetch.call_args_list],
            [datetime.date(2026, 8, 27), datetime.date(2026, 8, 26)],
        )
        mock_insert.assert_not_called()

    def test_preopen_holiday_lookback_cannot_regress_current_db_value(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 18, 3, 0))
        friday_rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        current_rates = {"usd-krw": 1383.0, "jpy-krw": 868.0, "eur-krw": 1611.0}
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            side_effect=[None, (friday_rates, "23:59:51")],
        ) as mock_fetch, patch.object(
            ibk.crud,
            "get_last_bank_rates_with_ts",
            return_value=_last_info(
                current_rates,
                _utc_naive(ibk.KST.localize(datetime.datetime(2026, 8, 17, 10, 0))),
            ),
        ), patch.object(ibk.crud, "insert_bank_rates_into_db") as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.PRESERVED)
        self.assertEqual(
            [item.args[0] for item in mock_fetch.call_args_list],
            [datetime.date(2026, 8, 17), datetime.date(2026, 8, 14)],
        )
        mock_insert.assert_not_called()

    def test_older_service_date_can_catch_up_a_newer_official_completion(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 18, 3, 0))
        intermediate_rates = {"usd-krw": 1381.0, "jpy-krw": 866.0, "eur-krw": 1609.0}
        friday_final_rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        db_saved_at = _utc_naive(
            ibk.KST.localize(datetime.datetime(2026, 8, 15, 4, 0))
        )
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            side_effect=[None, (friday_final_rates, "05:59:41")],
        ), patch.object(
            ibk.crud,
            "get_last_bank_rates_with_ts",
            return_value=_last_info(intermediate_rates, db_saved_at),
        ), patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
            return_value=3,
        ) as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.OBSERVED)
        mock_insert.assert_called_once_with(
            db=db,
            current_rates=friday_final_rates,
            bank_name=ibk.BANK_NAME,
        )

    def test_partial_database_bootstraps_missing_pair_without_regressing_newer_pairs(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 27, 10, 0))
        historical_rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        newer_saved_at = _utc_naive(
            ibk.KST.localize(datetime.datetime(2026, 8, 27, 9, 0))
        )
        partial_last = {
            "usd-krw": {"rate": 1383.0, "timestamp": newer_saved_at},
            "jpy-krw": {"rate": 868.0, "timestamp": newer_saved_at},
            "eur-krw": {"rate": None, "timestamp": None},
        }
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            side_effect=[None, (historical_rates, "05:59:55")],
        ), patch.object(
            ibk.crud,
            "get_last_bank_rates_with_ts",
            return_value=partial_last,
        ), patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
            return_value=1,
        ) as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.OBSERVED)
        mock_insert.assert_called_once_with(
            db=db,
            current_rates={"eur-krw": historical_rates["eur-krw"]},
            bank_name=ibk.BANK_NAME,
        )

    def test_saturday_early_morning_reads_friday_service_date(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 22, 3, 0))
        rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        with patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            return_value=(rates, "05:59:41"),
        ) as mock_fetch, patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
            return_value=0,
        ):
            ok = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(ok, ibk.DatedRequestOutcome.OBSERVED)
        self.assertEqual(mock_fetch.call_args.args[0], datetime.date(2026, 8, 21))

    def test_soft_lookback_budget_stops_new_candidates(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 18, 7, 50))
        with patch.object(ibk.time, "monotonic", side_effect=[0.0, 1.0, 13.0]), patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            return_value=None,
        ) as mock_fetch:
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.FALLBACK)
        self.assertEqual(mock_fetch.call_count, 1)
        self.assertEqual(mock_fetch.call_args.kwargs["timeout"], ibk.DEFAULT_TIMEOUT)

    def test_soft_budget_passes_remaining_time_below_default_timeout(self):
        db = MagicMock()
        reference_time = ibk.KST.localize(datetime.datetime(2026, 8, 18, 7, 50))
        rates = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
        with patch.object(ibk.time, "monotonic", side_effect=[0.0, 9.0]), patch.object(
            ibk,
            "_fetch_ibk_rates_for_date",
            return_value=(rates, "23:59:51"),
        ) as mock_fetch, patch.object(
            ibk.crud,
            "insert_bank_rates_into_db",
            return_value=0,
        ):
            outcome = ibk.try_crawl_with_dated_requests(db, reference_time)

        self.assertIs(outcome, ibk.DatedRequestOutcome.OBSERVED)
        self.assertEqual(mock_fetch.call_args.kwargs["timeout"], 3.0)

    def test_missing_table_outside_preopen_window_falls_back_through_real_parser(self):
        """개장 전 제한 구간 밖의 표 누락은 lookback 없이 즉시 FALLBACK.

        `_fetch_ibk_rates_for_date`를 mock하지 않고 실제 HTML 판별기를 통과시켜,
        표 부재의 제한적 허용이 다른 시간대까지 넓어지지 않도록 잠근다.
        """
        db = MagicMock()
        broken = """
            <html><body>
              <input id="inDate" value="2026.08.26">
              <div>환율 조회 준비 중</div>
            </body></html>
        """
        with patch.object(
            ibk.requests, "post", return_value=_response(broken)
        ) as mock_post, patch.object(
            ibk.crud, "insert_bank_rates_into_db"
        ) as mock_insert:
            outcome = ibk.try_crawl_with_dated_requests(
                db,
                ibk.KST.localize(datetime.datetime(2026, 8, 27, 3, 0)),
            )

        self.assertIs(outcome, ibk.DatedRequestOutcome.FALLBACK)
        mock_insert.assert_not_called()
        # 응답 이상은 공휴일이 아니므로 더 이전 기준일로 내려가지 않는다.
        self.assertEqual(mock_post.call_count, 1)


class TestIbkFallbackOrder(unittest.TestCase):
    @staticmethod
    def _datetime_module(now):
        module = MagicMock()
        module.datetime.now.return_value = now
        module.date = datetime.date
        module.timedelta = datetime.timedelta
        return module

    def _run(
        self,
        now,
        *,
        current_ok=False,
        dated_outcome=ibk.DatedRequestOutcome.FALLBACK,
    ):
        db = MagicMock()
        stack = (
            patch.object(ibk, "datetime", self._datetime_module(now)),
            patch.object(ibk, "SessionLocal", return_value=db),
            patch.object(ibk, "try_crawl_with_requests", return_value=current_ok),
            patch.object(ibk, "try_crawl_with_dated_requests", return_value=dated_outcome),
            patch.object(ibk, "crawl_and_save_ibk_routine_selenium", return_value=0),
        )
        with stack[0], stack[1], stack[2] as current, stack[3] as history, stack[4] as selenium:
            ibk.crawl_and_save_ibk_bank_exchange_rates()
        return db, current, history, selenium

    def test_historical_success_avoids_selenium(self):
        now = ibk.KST.localize(datetime.datetime(2026, 8, 27, 3, 0, 34))
        db, current, history, selenium = self._run(
            now,
            dated_outcome=ibk.DatedRequestOutcome.OBSERVED,
        )
        current.assert_not_called()
        history.assert_called_once_with(db, reference_time=now)
        selenium.assert_not_called()
        db.close.assert_called_once_with()

    def test_preserved_outcome_does_not_fall_through_to_selenium(self):
        now = ibk.KST.localize(datetime.datetime(2026, 8, 27, 3, 0, 34))
        db, _, history, selenium = self._run(
            now,
            dated_outcome=ibk.DatedRequestOutcome.PRESERVED,
        )
        history.assert_called_once_with(db, reference_time=now)
        selenium.assert_not_called()
        db.close.assert_called_once_with()

    def test_midnight_window_runs_dated_request_but_still_blocks_selenium(self):
        now = ibk.KST.localize(datetime.datetime(2026, 8, 27, 0, 2, 34))
        db, current, history, selenium = self._run(now)
        current.assert_not_called()
        history.assert_called_once_with(db, reference_time=now)
        selenium.assert_not_called()
        db.close.assert_called_once_with()

    def test_selenium_remains_fallback_after_midnight_window(self):
        now = ibk.KST.localize(datetime.datetime(2026, 8, 27, 0, 5, 0))
        db, current, history, selenium = self._run(now)
        current.assert_not_called()
        history.assert_called_once_with(db, reference_time=now)
        # 위치 인자는 그대로 고정한다. `run_started_at` 은 shadow 예산 가드용 회차 기준이라
        # 폴백 순서 계약과 무관하지만, **전달된다는 사실 자체**는 따로 잠근다
        # (`test_ibk_legacy_result.py::test_selenium_call_carries_the_run_anchor`).
        assert selenium.call_count == 1
        assert selenium.call_args.args == (ibk.IBK_BANK_URL, ibk.IBK_BANK_SELECTORS, db)
        assert set(selenium.call_args.kwargs) == {"run_started_at"}
        db.close.assert_called_once_with()

    def test_after_service_day_start_current_get_still_runs_first(self):
        now = ibk.KST.localize(datetime.datetime(2026, 8, 27, 8, 0, 0))
        db, current, history, selenium = self._run(now, current_ok=True)
        current.assert_called_once_with(db, reference_time=now)
        history.assert_not_called()
        selenium.assert_not_called()
        db.close.assert_called_once_with()

    def test_after_service_day_start_current_failure_uses_dated_request(self):
        now = ibk.KST.localize(datetime.datetime(2026, 8, 27, 8, 0, 0))
        db, current, history, selenium = self._run(
            now,
            current_ok=False,
            dated_outcome=ibk.DatedRequestOutcome.OBSERVED,
        )
        current.assert_called_once_with(db, reference_time=now)
        history.assert_called_once_with(db, reference_time=now)
        selenium.assert_not_called()
        db.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
