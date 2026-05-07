"""app/sources/kis_master.py 단위 테스트 (PR6b-1).

마스터 파일 다운로드는 mock으로, parsing/select는 fixture로 검증.
운영 미연결 — 실제 KIS 서버 호출 X.

실행:
    python -m unittest tests.test_kis_master -v
"""
from __future__ import annotations

import io
import unittest
import zipfile
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from app.sources.kis_master import (
    KIS_MASTER_URL,
    USD_FUTURES_NAME_PREFIX,
    ContractInfo,
    _compute_expiry_date,
    _extract_contract_month,
    fetch_commodity_future_master,
    parse_commodity_future_master,
    resolve_front_month_usd_futures,
    select_active_usd_futures_contract,
    select_front_month_usd_futures,
)


# ---------------------------------------------------------------------------
# Constants invariant
# ---------------------------------------------------------------------------

class TestConstants(unittest.TestCase):

    def test_master_url_https(self):
        self.assertTrue(KIS_MASTER_URL.startswith("https://"))
        self.assertIn("fo_com_code.mst.zip", KIS_MASTER_URL)

    def test_usd_prefix(self):
        self.assertEqual(USD_FUTURES_NAME_PREFIX, "미국달러 F")


# ---------------------------------------------------------------------------
# _extract_contract_month
# ---------------------------------------------------------------------------

class TestExtractContractMonth(unittest.TestCase):

    def test_valid_yyyymm(self):
        self.assertEqual(_extract_contract_month("미국달러 F 202605"), "202605")
        self.assertEqual(_extract_contract_month("엔 F 202612"), "202612")

    def test_no_yyyymm(self):
        self.assertIsNone(_extract_contract_month("미국달러"))
        self.assertIsNone(_extract_contract_month(""))

    def test_short_digit_not_yyyymm(self):
        self.assertIsNone(_extract_contract_month("미국달러 F 12345"))  # 5자
        self.assertIsNone(_extract_contract_month("미국달러 F 1234567"))  # 7자

    def test_non_digit_at_end(self):
        self.assertIsNone(_extract_contract_month("미국달러 F XYZABC"))


# ---------------------------------------------------------------------------
# _compute_expiry_date — 셋째 월요일
# ---------------------------------------------------------------------------

class TestComputeExpiryDate(unittest.TestCase):

    def test_2026_05_third_monday_is_18(self):
        """2026-05-01=금, 첫 월요일 5/4, 셋째 월요일 5/18 (KIS smoke 검증값)."""
        self.assertEqual(_compute_expiry_date("202605"), date(2026, 5, 18))

    def test_2026_06_third_monday_is_15(self):
        """2026-06-01=월요일, 첫 월요일 6/1, 셋째 월요일 6/15."""
        self.assertEqual(_compute_expiry_date("202606"), date(2026, 6, 15))

    def test_2026_07_third_monday_is_20(self):
        """2026-07-01=수, 첫 월요일 7/6, 셋째 월요일 7/20."""
        self.assertEqual(_compute_expiry_date("202607"), date(2026, 7, 20))

    def test_2026_12(self):
        """2026-12-01=화, 첫 월요일 12/7, 셋째 월요일 12/21."""
        self.assertEqual(_compute_expiry_date("202612"), date(2026, 12, 21))

    def test_invalid_format(self):
        self.assertIsNone(_compute_expiry_date("2026"))
        self.assertIsNone(_compute_expiry_date("20260X"))
        self.assertIsNone(_compute_expiry_date("ABCDEF"))

    def test_invalid_month(self):
        self.assertIsNone(_compute_expiry_date("202613"))  # 13월
        self.assertIsNone(_compute_expiry_date("202600"))  # 0월


# ---------------------------------------------------------------------------
# ContractInfo
# ---------------------------------------------------------------------------

class TestContractInfo(unittest.TestCase):

    def test_is_usd_krw_futures_true(self):
        c = ContractInfo(
            short_code="A75605",
            standard_code="KR4A75650007",
            name="미국달러 F 202605",
            contract_month="202605",
            expiry_date=date(2026, 5, 18),
        )
        self.assertTrue(c.is_usd_krw_futures())

    def test_is_usd_krw_futures_false_for_other(self):
        for name in ("엔 F 202605", "유로 F 202605", "위안 F 202605", "금 F 202605"):
            c = ContractInfo(
                short_code="X12345",
                standard_code="KR4X12340000",
                name=name,
                contract_month="202605",
                expiry_date=date(2026, 5, 18),
            )
            self.assertFalse(c.is_usd_krw_futures(), f"{name} should not be USD")

    def test_dataclass_frozen(self):
        c = ContractInfo("A1", "K1", "미국달러 F 202605", "202605", date(2026, 5, 18))
        with self.assertRaises(Exception):  # FrozenInstanceError
            c.short_code = "X"  # type: ignore


# ---------------------------------------------------------------------------
# parse_commodity_future_master — fixed-width parsing
# ---------------------------------------------------------------------------

class TestParseCommodityFutureMaster(unittest.TestCase):

    @staticmethod
    def _make_line(short_code: str, standard_code: str, name: str, prefix="C5") -> str:
        """KIS 마스터 line 형식 fixture.

        [0:1] 상품구분 / [1:2] 상품종류 / [2:11] 단축코드 (9자) /
        [11:23] 표준코드 (12자) / [23:55] 종목명 (32자) / [55:] tail.

        한글 1자 = 1 char (Python str slicing 기준). cp949 byte는 무관.
        """
        return (
            prefix[0]
            + prefix[1]
            + short_code.ljust(9)
            + standard_code.ljust(12)
            + name.ljust(32)
            + "tail"
        )

    def test_parse_single_usd_futures(self):
        line = self._make_line("A75605", "KR4A75650007", "미국달러 F 202605")
        raw = line.encode("cp949")
        contracts = parse_commodity_future_master(raw)
        self.assertEqual(len(contracts), 1)
        c = contracts[0]
        self.assertEqual(c.short_code, "A75605")
        self.assertEqual(c.standard_code, "KR4A75650007")
        self.assertEqual(c.name, "미국달러 F 202605")
        self.assertEqual(c.contract_month, "202605")
        self.assertEqual(c.expiry_date, date(2026, 5, 18))
        self.assertTrue(c.is_usd_krw_futures())

    def test_parse_multiple_with_other_futures(self):
        lines = "\n".join([
            self._make_line("A75605", "KR4A75650007", "미국달러 F 202605"),
            self._make_line("A75606", "KR4A75660006", "미국달러 F 202606"),
            self._make_line("Z00001", "KR4Z00010003", "금 F 202605"),  # 다른 상품
        ])
        raw = lines.encode("cp949")
        contracts = parse_commodity_future_master(raw)
        self.assertEqual(len(contracts), 3)
        usd = [c for c in contracts if c.is_usd_krw_futures()]
        self.assertEqual(len(usd), 2)
        self.assertEqual([c.short_code for c in usd], ["A75605", "A75606"])

    def test_parse_skips_short_lines(self):
        lines = "\n".join([
            "tooshort",  # 55자 미만 — 스킵
            self._make_line("A75605", "KR4A75650007", "미국달러 F 202605"),
        ])
        raw = lines.encode("cp949")
        contracts = parse_commodity_future_master(raw)
        self.assertEqual(len(contracts), 1)

    def test_parse_skips_no_yyyymm_in_name(self):
        line = self._make_line("X00001", "KR4X00010002", "기준종목")  # YYYYMM 없음
        raw = line.encode("cp949")
        contracts = parse_commodity_future_master(raw)
        self.assertEqual(len(contracts), 0)


# ---------------------------------------------------------------------------
# select_front_month_usd_futures
# ---------------------------------------------------------------------------

class TestSelectFrontMonth(unittest.TestCase):

    def setUp(self):
        self.contracts = [
            ContractInfo("A75605", "KR4A75650007", "미국달러 F 202605",
                         "202605", date(2026, 5, 18)),
            ContractInfo("A75606", "KR4A75660006", "미국달러 F 202606",
                         "202606", date(2026, 6, 15)),
            ContractInfo("A75607", "KR4A75670005", "미국달러 F 202607",
                         "202607", date(2026, 7, 20)),
            # 다른 상품선물 — front-month 후보 X
            ContractInfo("Z00001", "KR4Z00010003", "금 F 202605",
                         "202605", date(2026, 5, 18)),
        ]

    def test_today_before_first_expiry(self):
        """2026-05-04 → A75605 (5/18 만기)."""
        front = select_front_month_usd_futures(
            self.contracts, today=date(2026, 5, 4)
        )
        self.assertEqual(front.short_code, "A75605")

    def test_today_at_first_expiry(self):
        """2026-05-18 만기일 당일 → A75605 (>=today 포함)."""
        front = select_front_month_usd_futures(
            self.contracts, today=date(2026, 5, 18)
        )
        self.assertEqual(front.short_code, "A75605")

    def test_today_after_first_expiry_rolls_to_next(self):
        """2026-05-19 (만기 다음날) → A75606 (자동 rollover)."""
        front = select_front_month_usd_futures(
            self.contracts, today=date(2026, 5, 19)
        )
        self.assertEqual(front.short_code, "A75606")

    def test_today_after_all_expired_returns_none(self):
        """모든 만기 지남 → None."""
        front = select_front_month_usd_futures(
            self.contracts, today=date(2027, 1, 1)
        )
        self.assertIsNone(front)

    def test_empty_contracts(self):
        self.assertIsNone(
            select_front_month_usd_futures([], today=date(2026, 5, 4))
        )

    def test_only_other_futures(self):
        only_gold = [
            ContractInfo("Z00001", "KR4Z00010003", "금 F 202605",
                         "202605", date(2026, 5, 18)),
        ]
        self.assertIsNone(
            select_front_month_usd_futures(only_gold, today=date(2026, 5, 4))
        )


# ---------------------------------------------------------------------------
# select_active_usd_futures_contract — intraday rollover (PR6c-1)
# ---------------------------------------------------------------------------

class TestSelectActiveContract(unittest.TestCase):
    """PR6c-2d-1 — 만기일 07:00 KST swap point. 사용자 대표 월물 선제 전환.

    이전 정책 (PR6c-1): 만기일 11:30:00까지 만기 종목, 11:30:01 이후 다음.
    새 정책 (2026-05-07): 만기일 07:00:00 이상이면 다음 월물.
    동기: 만기 직전 영업일 야간장 종료 후 사용자 대표 월물 전환 +
    06:00 boundary race 회피 (07:00 휴장 한가운데).
    """

    def setUp(self):
        self.contracts = [
            ContractInfo("A75605", "KR4A75650007", "미국달러 F 202605",
                         "202605", date(2026, 5, 18)),
            ContractInfo("A75606", "KR4A75660006", "미국달러 F 202606",
                         "202606", date(2026, 6, 15)),
            ContractInfo("A75607", "KR4A75670005", "미국달러 F 202607",
                         "202607", date(2026, 7, 20)),
            # 다른 상품선물 — USD futures 후보 X
            ContractInfo("Z00001", "KR4Z00010003", "금 F 202605",
                         "202605", date(2026, 5, 18)),
        ]

    # 비-만기일 → 가장 빠른 만기 USD futures
    def test_non_expiry_day_morning(self):
        now = datetime(2026, 5, 4, 9, 0, 0)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75605",
        )

    def test_non_expiry_day_evening(self):
        """비-만기일 야간장 — swap 정책은 만기일에만 적용."""
        now = datetime(2026, 5, 4, 18, 0, 0)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75605",
        )

    def test_expiry_eve_late_night_keeps_front(self):
        """만기 전날 23:59 — swap point 미도달, 만기 종목 유지."""
        now = datetime(2026, 5, 17, 23, 59, 59)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75605",
        )

    # 만기일 swap_point(07:00) 이전 → 만기 종목 유지
    def test_expiry_day_at_06_00_keeps_front(self):
        """만기일 06:00 정각 — 야간장 종료 시각, swap_point 미도달."""
        now = datetime(2026, 5, 18, 6, 0, 0)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75605",
        )

    def test_expiry_day_at_06_59_keeps_front(self):
        """만기일 06:59:59 — swap_point 1초 전, 만기 종목 유지."""
        now = datetime(2026, 5, 18, 6, 59, 59)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75605",
        )

    # 만기일 swap_point(07:00) 정각 이상 → 다음 월물 (inclusive)
    def test_expiry_day_at_07_00_swaps(self):
        """만기일 07:00:00 정각 — swap_point inclusive, 다음 월물."""
        now = datetime(2026, 5, 18, 7, 0, 0)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75606",
        )

    def test_expiry_day_at_07_00_01_after_swap(self):
        """만기일 07:00:01 — swap 직후."""
        now = datetime(2026, 5, 18, 7, 0, 1)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75606",
        )

    def test_expiry_day_at_08_30_after_swap(self):
        """만기일 08:30 — CF 정규장 시작, 이미 다음 월물."""
        now = datetime(2026, 5, 18, 8, 30, 0)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75606",
        )

    def test_expiry_day_at_11_30_after_swap(self):
        """만기일 11:30 — 거래소 만기 종료 시각이지만 우리는 이미 다음 월물."""
        now = datetime(2026, 5, 18, 11, 30, 0)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75606",
        )

    def test_expiry_day_noon_rollover(self):
        now = datetime(2026, 5, 18, 12, 0, 0)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75606",
        )

    def test_expiry_day_night_session_rollover(self):
        """만기일 18:00 야간장 — 다음 월물."""
        now = datetime(2026, 5, 18, 18, 0, 0)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75606",
        )

    # 만기 다음날 (5/19) → 다음 월물 (만기 종목 자동 제외)
    def test_day_after_expiry_uses_date_level(self):
        now = datetime(2026, 5, 19, 9, 0, 0)
        self.assertEqual(
            select_active_usd_futures_contract(self.contracts, now).short_code,
            "A75606",
        )

    # edge cases
    def test_no_usd_futures_returns_none(self):
        only_gold = [
            ContractInfo("Z00001", "KR4Z00010003", "금 F 202605",
                         "202605", date(2026, 5, 18)),
        ]
        now = datetime(2026, 5, 18, 12, 0, 0)
        self.assertIsNone(select_active_usd_futures_contract(only_gold, now))

    def test_after_swap_no_next_contract(self):
        """만기일 swap_point 이후 + 다음 월물 미등록 → None."""
        single = [self.contracts[0]]  # A75605만
        now = datetime(2026, 5, 18, 12, 0, 0)
        self.assertIsNone(select_active_usd_futures_contract(single, now))

    def test_all_contracts_expired(self):
        now = datetime(2027, 1, 1, 9, 0, 0)
        self.assertIsNone(select_active_usd_futures_contract(self.contracts, now))


# ---------------------------------------------------------------------------
# fetch_commodity_future_master — network mock
# ---------------------------------------------------------------------------

class TestFetchCommodityFutureMaster(unittest.TestCase):

    @staticmethod
    def _make_zip_bytes(content: bytes, name: str = "fo_com_code.mst") -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(name, content)
        return buf.getvalue()

    @patch("app.sources.kis_master.requests.get")
    def test_fetch_success(self, mock_get):
        mst_content = "C5A75605   KR4A75650007미국달러 F 202605               tail".encode("cp949")
        zip_bytes = self._make_zip_bytes(mst_content)
        mock_response = MagicMock()
        mock_response.content = zip_bytes
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        with self.assertLogs("app.sources.kis_master", level="INFO") as cm:
            result = fetch_commodity_future_master()
        self.assertEqual(result, mst_content)
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args[0][0], KIS_MASTER_URL)
        self.assertTrue(any("downloading" in m for m in cm.output))

    @patch("app.sources.kis_master.requests.get")
    def test_fetch_no_mst_in_zip(self, mock_get):
        """zip 안에 .mst 파일 없으면 RuntimeError."""
        zip_bytes = self._make_zip_bytes(b"data", name="other.txt")
        mock_response = MagicMock()
        mock_response.content = zip_bytes
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        with self.assertLogs("app.sources.kis_master", level="INFO"):
            with self.assertRaises(RuntimeError):
                fetch_commodity_future_master()


# ---------------------------------------------------------------------------
# resolve_front_month_usd_futures — fetch + parse + select 통합
# ---------------------------------------------------------------------------

class TestResolveFrontMonthUsdFutures(unittest.TestCase):

    @patch("app.sources.kis_master.fetch_commodity_future_master")
    def test_resolve_success(self, mock_fetch):
        line = (
            "C5"
            + "A75605".ljust(9)
            + "KR4A75650007".ljust(12)
            + "미국달러 F 202605".ljust(32)
            + "tail"
        )
        mock_fetch.return_value = line.encode("cp949")
        with self.assertLogs("app.sources.kis_master", level="INFO") as cm:
            result = resolve_front_month_usd_futures(today=date(2026, 5, 4))
        self.assertIsNotNone(result)
        self.assertEqual(result.short_code, "A75605")
        self.assertTrue(any("front-month USD futures" in m for m in cm.output))

    @patch("app.sources.kis_master.fetch_commodity_future_master")
    def test_resolve_fetch_failure_returns_none(self, mock_fetch):
        """fetch 실패 → None + warning (예외 X)."""
        mock_fetch.side_effect = RuntimeError("network error")
        with self.assertLogs("app.sources.kis_master", level="WARNING") as cm:
            result = resolve_front_month_usd_futures()
        self.assertIsNone(result)
        self.assertTrue(any("fetch failed" in m for m in cm.output))

    @patch("app.sources.kis_master.fetch_commodity_future_master")
    def test_resolve_no_usd_futures_returns_none(self, mock_fetch):
        """USD futures 없음 → None + warning."""
        line = (
            "C5"
            + "Z00001".ljust(9)
            + "KR4Z00010003".ljust(12)
            + "금 F 202605".ljust(32)
            + "tail"
        )
        mock_fetch.return_value = line.encode("cp949")
        with self.assertLogs("app.sources.kis_master", level="WARNING") as cm:
            result = resolve_front_month_usd_futures(today=date(2026, 5, 4))
        self.assertIsNone(result)
        self.assertTrue(any("no front-month" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
