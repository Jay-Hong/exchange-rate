"""app/sources/kis_futures 단위 테스트 (stdlib unittest).

운영 코드 import / DB / Redis 의존 없음. 순수 로직만 검증.

실행:
    python -m unittest tests.test_kis_futures -v
또는:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from app.sources.kis_futures import (
    H0CFASP0_COLUMNS,
    H0CFCNT0_COLUMNS,
    H0MFASP0_COLUMNS,
    H0MFCNT0_COLUMNS,
    get_active_session,
    is_krx_business_day,
    is_krx_night_session_open,
    night_session_start_date,
    parse_h0cfasp0_payload,
    parse_h0cfcnt0_payload,
    parse_h0mfasp0_payload,
    parse_h0mfcnt0_payload,
)

# 세션 창(08:30/15:45/17:50/06:00) 자체를 검증하는 테스트가 만기 분기에 걸리지
# 않도록 쓰는 **먼 미래 만기**. contract_expiry_date 필수화(2026-08-16) 이후
# "만기와 무관한 세션 판정"을 표현하는 표준 인자다.
FAR_FUTURE_EXPIRY = date(2099, 12, 21)


# ---------------------------------------------------------------------------
# Parser tests — 어제 raw smoke 샘플 기반 (검증된 첫 12 + 9 필드)
# ---------------------------------------------------------------------------

class TestParseH0CFCNT0(unittest.TestCase):
    """체결 H0CFCNT0 parser — 50 필드.

    raw 샘플은 어제 (2026-05-04 09:38) 주간장 smoke에서 수신한 첫 12 필드 검증.
    나머지 필드는 길이 일치 + columns 매핑 위치만 검증.
    """

    def setUp(self):
        # 어제 09:38:32 KST 수신 샘플 (smoke prefix 120자 + 추가 필드 padding)
        # 첫 12 필드는 broker 앱과 cross-check 완료, 나머지는 매핑 위치만 검증.
        self.sample_data = "^".join([
            "A75605",            # 0  futs_shrn_iscd
            "093832",            # 1  bsop_hour
            "-12.70012324",      # 2  futs_prdy_vrss
            "5",                 # 3  prdy_vrss_sign (5=하락)
            "-0.86005001",       # 4  futs_prdy_ctrt
            "1470.60002559",     # 5  futs_prpr ★
            "1473.10002559",     # 6  futs_oprc
            "1473.30009883",     # 7  futs_hgpr
            "1469.50005000",     # 8  futs_lwpr
            "10",                # 9  last_cnqn
            "122927",            # 10 acml_vol
            "1807719093000",     # 11 acml_tr_pbmn
        ] + ["0"] * (50 - 12))   # 나머지 38 필드는 placeholder

    def test_parses_50_fields(self):
        result = parse_h0cfcnt0_payload(self.sample_data)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 50)

    def test_columns_count_invariant(self):
        self.assertEqual(len(H0CFCNT0_COLUMNS), 50)

    def test_verified_first_12_fields(self):
        result = parse_h0cfcnt0_payload(self.sample_data)
        self.assertEqual(result["futs_shrn_iscd"], "A75605")
        self.assertEqual(result["bsop_hour"], "093832")
        self.assertEqual(result["futs_prdy_vrss"], "-12.70012324")
        self.assertEqual(result["prdy_vrss_sign"], "5")
        self.assertEqual(result["futs_prdy_ctrt"], "-0.86005001")
        self.assertEqual(result["futs_prpr"], "1470.60002559")  # 핵심 — 현재가
        self.assertEqual(result["futs_oprc"], "1473.10002559")
        self.assertEqual(result["futs_hgpr"], "1473.30009883")
        self.assertEqual(result["futs_lwpr"], "1469.50005000")
        self.assertEqual(result["last_cnqn"], "10")
        self.assertEqual(result["acml_vol"], "122927")
        self.assertEqual(result["acml_tr_pbmn"], "1807719093000")

    def test_returns_none_for_short_payload(self):
        short = "A75605^093832^"  # 2 필드만
        self.assertIsNone(parse_h0cfcnt0_payload(short))

    def test_returns_none_for_empty(self):
        self.assertIsNone(parse_h0cfcnt0_payload(""))


class TestParseH0CFASP0(unittest.TestCase):
    """호가 H0CFASP0 parser — 38 필드.

    raw 샘플은 어제 09:38:32 호가 smoke 첫 9 필드 검증.
    """

    def setUp(self):
        # 첫 9 필드 (종목+시각+ask1-5+bid1-3) cross-check, 나머지 padding
        self.sample_data = "^".join([
            "A75605",            # 0  futs_shrn_iscd
            "093832",            # 1  bsop_hour
            "1470.60002559",     # 2  futs_askp1 ★ 매도1 (가장 낮은 매도가)
            "1470.70000117",     # 3  futs_askp2
            "1470.80009883",     # 4  futs_askp3
            "1470.90007441",     # 5  futs_askp4
            "1471.00005000",     # 6  futs_askp5
            "1470.50005000",     # 7  futs_bidp1 ★ 매수1 (가장 높은 매수가)
            "1470.40007441",     # 8  futs_bidp2
            "1470.30000000",     # 9  futs_bidp3
        ] + ["0"] * (38 - 10))

    def test_parses_38_fields(self):
        result = parse_h0cfasp0_payload(self.sample_data)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 38)

    def test_columns_count_invariant(self):
        self.assertEqual(len(H0CFASP0_COLUMNS), 38)

    def test_askp_bidp_price_relation(self):
        result = parse_h0cfasp0_payload(self.sample_data)
        # 매도호가1 (1470.60) > 매수호가1 (1470.50) — 가격 관계 정상
        self.assertGreater(
            float(result["futs_askp1"]), float(result["futs_bidp1"])
        )
        # 매도호가 1-5 오름차순
        asks = [float(result[f"futs_askp{i}"]) for i in range(1, 6)]
        self.assertEqual(asks, sorted(asks))
        # 매수호가 1-3 내림차순 (4-5는 padding 0이라 검증 제외)
        bids = [float(result[f"futs_bidp{i}"]) for i in range(1, 4)]
        self.assertEqual(bids, sorted(bids, reverse=True))


class TestParseH0MFCNT0(unittest.TestCase):
    """야간 체결 H0MFCNT0 parser — 49 필드 (주간 -1, dscs_bltr_acml_qty 누락).

    raw 샘플은 2026-05-04 18:03:32 KST 야간장 smoke 수신값.
    """

    def setUp(self):
        # 어제 18:03:32 야간 smoke recv#37 첫 12 필드 + padding
        self.sample_data = "^".join([
            "A75605",            # 0  futs_shrn_iscd
            "180332",            # 1  bsop_hour
            "6.30",              # 2  futs_prdy_vrss
            "2",                 # 3  prdy_vrss_sign (2=상승)
            "0.43",              # 4  futs_prdy_ctrt
            "1468.50",           # 5  futs_prpr ★
            "1468.90",           # 6  futs_oprc
            "1469.30",           # 7  futs_hgpr
            "1468.50",           # 8  futs_lwpr
            "1",                 # 9  last_cnqn
            "2674",              # 10 acml_vol
            "39275780",          # 11 acml_tr_pbmn
        ] + ["0"] * (49 - 12))

    def test_parses_49_fields(self):
        result = parse_h0mfcnt0_payload(self.sample_data)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 49)

    def test_columns_count_invariant(self):
        self.assertEqual(len(H0MFCNT0_COLUMNS), 49)

    def test_night_columns_one_less_than_day(self):
        """야간 체결 columns은 주간 (50개) - 1 = 49개 (dscs_bltr_acml_qty 누락)."""
        self.assertEqual(len(H0CFCNT0_COLUMNS) - len(H0MFCNT0_COLUMNS), 1)
        # 누락된 필드 확인
        day_set = set(H0CFCNT0_COLUMNS)
        night_set = set(H0MFCNT0_COLUMNS)
        self.assertEqual(day_set - night_set, {"dscs_bltr_acml_qty"})

    def test_verified_first_12_fields(self):
        result = parse_h0mfcnt0_payload(self.sample_data)
        self.assertEqual(result["futs_shrn_iscd"], "A75605")
        self.assertEqual(result["bsop_hour"], "180332")
        self.assertEqual(result["futs_prdy_vrss"], "6.30")
        self.assertEqual(result["prdy_vrss_sign"], "2")  # 상승
        self.assertEqual(result["futs_prpr"], "1468.50")  # 야간 현재가
        # 야간 시가/고가/저가 관계 검증
        oprc = float(result["futs_oprc"])
        hgpr = float(result["futs_hgpr"])
        lwpr = float(result["futs_lwpr"])
        self.assertGreaterEqual(hgpr, oprc)
        self.assertGreaterEqual(oprc, lwpr)


class TestParseH0MFASP0(unittest.TestCase):
    """야간 호가 H0MFASP0 parser — 38 필드 (주간 H0CFASP0와 동일)."""

    def setUp(self):
        # 어제 18:03:32 야간 smoke recv#35 첫 12 필드 + padding
        self.sample_data = "^".join([
            "A75605",            # 0
            "180332",            # 1
            "1468.70",           # 2  futs_askp1 ★
            "1468.80",           # 3
            "1468.90",           # 4
            "1469.00",           # 5
            "1469.10",           # 6
            "1468.50",           # 7  futs_bidp1 ★
            "1468.40",           # 8
            "1468.30",           # 9
            "1468.20",           # 10
            "1468.10",           # 11
        ] + ["0"] * (38 - 12))

    def test_parses_38_fields(self):
        result = parse_h0mfasp0_payload(self.sample_data)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 38)

    def test_night_quote_columns_match_day(self):
        """야간 호가 columns은 주간 H0CFASP0와 동일."""
        self.assertEqual(H0MFASP0_COLUMNS, H0CFASP0_COLUMNS)

    def test_night_askp_bidp_relation(self):
        result = parse_h0mfasp0_payload(self.sample_data)
        # 매도호가1 (1468.70) > 매수호가1 (1468.50)
        self.assertGreater(
            float(result["futs_askp1"]), float(result["futs_bidp1"])
        )
        # 매도호가 1-5 오름차순
        asks = [float(result[f"futs_askp{i}"]) for i in range(1, 6)]
        self.assertEqual(asks, sorted(asks))


# ---------------------------------------------------------------------------
# Calendar tests — 외부 cross-check 완료된 케이스만
# ---------------------------------------------------------------------------

class TestKrxBusinessDay(unittest.TestCase):

    def test_2026_05_04_monday_is_business_day(self):
        """5/4 월요일 — 외부 cross-check 완료 (CalendarLabs KRX 휴장일 미포함)."""
        self.assertTrue(is_krx_business_day(date(2026, 5, 4)))

    def test_2026_05_05_childrens_day_is_holiday(self):
        """5/5 어린이날 — 정규 한국 공휴일."""
        self.assertFalse(is_krx_business_day(date(2026, 5, 5)))

    def test_2026_05_03_sunday(self):
        """일요일 — 주말."""
        self.assertFalse(is_krx_business_day(date(2026, 5, 3)))

    def test_2026_05_02_saturday(self):
        """토요일 — 주말."""
        self.assertFalse(is_krx_business_day(date(2026, 5, 2)))

    def test_2026_05_25_buddhas_birthday_substitute_is_holiday(self):
        """5/25 월요일 — 부처님오신날(5/24 일) 대체공휴일.

        운영 사고로 확인 (2026-05-25 15:45 KST에 stale 5/22 종가가 잘못 기록됨).
        kwatch.kr/markets/kr/trading-days cross-check 완료.
        """
        self.assertFalse(is_krx_business_day(date(2026, 5, 25)))

    def test_2026_07_17_constitution_day_is_holiday(self):
        """7/17 제헌절 — 2026 공휴일 재지정 (kr_holidays observed). 거래일 아님."""
        self.assertFalse(is_krx_business_day(date(2026, 7, 17)))

    def test_2026_12_31_year_end_closure_is_holiday(self):
        """12/31 KRX 연말 폐장 — kr_holidays에 없는 KRX 고유 규칙."""
        self.assertFalse(is_krx_business_day(date(2026, 12, 31)))

    def test_2026_12_30_is_business_day(self):
        """12/30 — 2026 최종 매매거래일 (연말 폐장 직전)."""
        self.assertTrue(is_krx_business_day(date(2026, 12, 30)))

    def test_year_end_closure_across_years(self):
        """연말 폐장 규칙(과거 연도): 12/31, 휴일이면 직전 매매거래일 = 휴장.

        2022 휴장 12/30·최종 12/29 / 2023 휴장 12/29·최종 12/28 / 2024 휴장 12/31·최종 12/30.
        """
        self.assertFalse(is_krx_business_day(date(2022, 12, 30)))
        self.assertTrue(is_krx_business_day(date(2022, 12, 29)))
        self.assertFalse(is_krx_business_day(date(2023, 12, 29)))
        self.assertTrue(is_krx_business_day(date(2023, 12, 28)))
        self.assertFalse(is_krx_business_day(date(2024, 12, 31)))
        self.assertTrue(is_krx_business_day(date(2024, 12, 30)))

    def test_dynamic_future_year_not_hardcoded(self):
        """연도 무관 동적 계산 — 2027 신정도 휴일 (구 2026 하드코딩이면 실패)."""
        self.assertFalse(is_krx_business_day(date(2027, 1, 1)))
        self.assertTrue(is_krx_business_day(date(2027, 1, 4)))


class TestNightSessionStartDate(unittest.TestCase):
    """야간 세션 시작일 helper — 06:00 경계를 microsecond 단위로 잠근다.

    `get_active_session`의 CM 분기(`t <= _NIGHT_END`)와 **정확히 같은 경계**여야
    한다. 두 곳이 어긋나면 gate 1과 gate 3이 서로 다른 날을 보게 되고, 그건
    구 gate 1이 토요일 새벽을 매주 오거부하던 결함과 같은 계열이다.
    """

    FAR = date(2099, 12, 21)

    def test_just_before_06_00_is_previous_day(self):
        now = datetime(2026, 8, 15, 5, 59, 59, 999999)  # Sat
        self.assertEqual(night_session_start_date(now), date(2026, 8, 14))

    def test_exactly_06_00_is_previous_day_inclusive(self):
        now = datetime(2026, 8, 15, 6, 0, 0)
        self.assertEqual(night_session_start_date(now), date(2026, 8, 14))

    def test_just_after_06_00_is_same_day(self):
        now = datetime(2026, 8, 15, 6, 0, 0, 1)
        self.assertEqual(night_session_start_date(now), date(2026, 8, 15))

    def test_evening_is_same_day(self):
        now = datetime(2026, 8, 14, 18, 0, 0)
        self.assertEqual(night_session_start_date(now), date(2026, 8, 14))

    def test_boundary_matches_get_active_session_cm_branch(self):
        """helper 경계 == `get_active_session` CM 분기 경계 (동시 검증)."""
        self.assertEqual(
            get_active_session(datetime(2026, 8, 15, 6, 0, 0), self.FAR), "CM"
        )
        self.assertIsNone(
            get_active_session(datetime(2026, 8, 15, 6, 0, 0, 1), self.FAR)
        )


class TestRemovedExpiryDayAxisContract(unittest.TestCase):
    """구 `is_expiry_day` / `KRX_2026_USDF_EXPIRY_DAYS` 축이 **실행 코드에 없다**.

    문자열 grep이 아니라 **AST**로 검사한다 — 테스트·주석이 심볼 이름을 문자열로
    언급하는 것과 실제 코드 사용을 구분해야 하기 때문이다(이 파일 자체가 그 예).
    검사 노드: Name / Import / ImportFrom / FunctionDef / Attribute.
    """

    REMOVED = ("is_expiry_day", "KRX_2026_USDF_EXPIRY_DAYS")
    # 실행 코드만 — tests/ 는 제외(이 계약을 서술하는 문자열이 정당하게 존재).
    SCAN_ROOTS = ("app", "scripts")

    def _scan(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        hits = []
        for sub in self.SCAN_ROOTS:
            for path in (root / sub).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name) and node.id in self.REMOVED:
                        hits.append(f"{path}:{node.lineno} Name({node.id})")
                    elif isinstance(node, ast.Attribute) and node.attr in self.REMOVED:
                        hits.append(f"{path}:{node.lineno} Attribute({node.attr})")
                    elif isinstance(node, ast.FunctionDef) and node.name in self.REMOVED:
                        hits.append(f"{path}:{node.lineno} FunctionDef({node.name})")
                    elif isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            if alias.name in self.REMOVED:
                                hits.append(f"{path}:{node.lineno} ImportFrom({alias.name})")
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name in self.REMOVED:
                                hits.append(f"{path}:{node.lineno} Import({alias.name})")
        return hits

    def test_removed_symbols_absent_from_executable_code(self):
        hits = self._scan()
        self.assertEqual(hits, [], f"제거 심볼이 실행 코드에 잔존: {hits}")


class TestGetActiveSessionRequiresExpiry(unittest.TestCase):
    """모든 `get_active_session()` 호출이 **만기 인자를 전달**한다 (AST trip-wire).

    None 허용을 남기면 fail-open이므로, signature 필수화와 호출부 준수를 함께
    잠근다. 인자를 빠뜨린 호출은 런타임에야 TypeError가 나고, 그 경로가 하필
    운영에서 드물게 도는 fallback이면 8/14 사고처럼 오래 숨는다.
    """

    def test_signature_has_no_default(self):
        import inspect

        sig = inspect.signature(get_active_session)
        param = sig.parameters["contract_expiry_date"]
        self.assertIs(param.default, inspect.Parameter.empty)

    def test_all_call_sites_pass_expiry_argument(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for sub in ("app", "scripts"):
            for path in (root / sub).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                    if name != "get_active_session":
                        continue
                    has_kw = any(k.arg == "contract_expiry_date" for k in node.keywords)
                    if len(node.args) < 2 and not has_kw:
                        offenders.append(f"{path}:{node.lineno}")
        self.assertEqual(offenders, [], f"만기 인자 없는 호출: {offenders}")


# ---------------------------------------------------------------------------
# Session tests — KRX "시작일 기준" 정책 케이스
# ---------------------------------------------------------------------------

class TestActiveSession(unittest.TestCase):

    # --- 정규세션 (CF) ---

    def test_monday_morning_regular(self):
        """5/4 월 09:38 — 정규세션 active."""
        now = datetime(2026, 5, 4, 9, 38, 32)
        self.assertEqual(get_active_session(now, FAR_FUTURE_EXPIRY), "CF")

    def test_monday_regular_open_auction(self):
        """5/4 월 08:30 — 정규 개장 단일가 시작."""
        now = datetime(2026, 5, 4, 8, 30, 0)
        self.assertEqual(get_active_session(now, FAR_FUTURE_EXPIRY), "CF")

    def test_monday_regular_close(self):
        """5/4 월 15:45 — 정규세션 종료 시각 (포함)."""
        now = datetime(2026, 5, 4, 15, 45, 0)
        self.assertEqual(get_active_session(now, FAR_FUTURE_EXPIRY), "CF")

    def test_monday_after_regular_break(self):
        """5/4 월 16:00 — 정규/야간 사이 break."""
        now = datetime(2026, 5, 4, 16, 0, 0)
        self.assertIsNone(get_active_session(now, FAR_FUTURE_EXPIRY))

    # --- 야간세션 (CM) — 시작일 기준 ---

    def test_monday_night_18_00(self):
        """5/4 월 18:00 — 야간 시작 (5/4 영업일이라 active)."""
        now = datetime(2026, 5, 4, 18, 0, 0)
        self.assertEqual(get_active_session(now, FAR_FUTURE_EXPIRY), "CM")

    def test_monday_night_17_50_auction(self):
        """5/4 월 17:50 — 야간 개장 단일가."""
        now = datetime(2026, 5, 4, 17, 50, 0)
        self.assertEqual(get_active_session(now, FAR_FUTURE_EXPIRY), "CM")

    def test_tuesday_dawn_extends_from_monday(self):
        """5/5 화 02:00 — 5/4 시작 야간장이 06:00까지 이어짐 (어린이날 휴장이어도)."""
        now = datetime(2026, 5, 5, 2, 0, 0)
        self.assertEqual(get_active_session(now, FAR_FUTURE_EXPIRY), "CM")

    def test_tuesday_dawn_06_00_end(self):
        """5/5 화 06:00 — 야간세션 종료 시각 (포함)."""
        now = datetime(2026, 5, 5, 6, 0, 0)
        self.assertEqual(get_active_session(now, FAR_FUTURE_EXPIRY), "CM")

    def test_tuesday_morning_after_night(self):
        """5/5 화 06:30 — 야간 종료 후 + 어린이날 휴장이라 None."""
        now = datetime(2026, 5, 5, 6, 30, 0)
        self.assertIsNone(get_active_session(now, FAR_FUTURE_EXPIRY))

    # --- 휴장 / 시작일이 휴일 ---

    def test_childrens_day_regular_closed(self):
        """5/5 화 어린이날 09:38 — 주간장 미실시."""
        now = datetime(2026, 5, 5, 9, 38, 0)
        self.assertIsNone(get_active_session(now, FAR_FUTURE_EXPIRY))

    def test_childrens_day_night_not_started(self):
        """5/5 화 18:00 — 시작일이 공휴일이라 야간장 미실시."""
        now = datetime(2026, 5, 5, 18, 0, 0)
        self.assertIsNone(get_active_session(now, FAR_FUTURE_EXPIRY))

    def test_sunday_evening_no_night(self):
        """5/3 일 18:00 — 시작일이 일요일이라 야간장 미실시."""
        now = datetime(2026, 5, 3, 18, 0, 0)
        self.assertIsNone(get_active_session(now, FAR_FUTURE_EXPIRY))

    def test_saturday_morning(self):
        """5/2 토 09:38 — 토요일 휴장."""
        now = datetime(2026, 5, 2, 9, 38, 0)
        self.assertIsNone(get_active_session(now, FAR_FUTURE_EXPIRY))

    # --- 만기일 정규세션 11:30 종료 (expiring contract 기준) ---
    # 2026-08-16: 구 버전은 만기 인자 없이 `is_expiry_day(today)` 캘린더 판정에
    # 의존했다. 그 축을 삭제했으므로 **운영 중인 계약의 만기일을 명시**한다.

    EXPIRY_2026_05 = date(2026, 5, 18)

    def test_expiry_day_before_11_30(self):
        """5/18 월 만기일 11:00 — 정규세션 active."""
        now = datetime(2026, 5, 18, 11, 0, 0)
        self.assertEqual(get_active_session(now, self.EXPIRY_2026_05), "CF")

    def test_expiry_day_at_11_30(self):
        """5/18 월 만기일 11:30 — 종료 시각 (포함)."""
        now = datetime(2026, 5, 18, 11, 30, 0)
        self.assertEqual(get_active_session(now, self.EXPIRY_2026_05), "CF")

    def test_expiry_day_after_11_30(self):
        """5/18 월 만기일 12:00 — 일반 거래일과 달리 정규세션 종료."""
        now = datetime(2026, 5, 18, 12, 0, 0)
        self.assertIsNone(get_active_session(now, self.EXPIRY_2026_05))

    def test_expiry_day_night_blocked_for_expiring_contract(self):
        """5/18 만기일 18:30 + expiring contract → 야간장도 차단.

        구 테스트는 `is_expiry_day` None 경로에서 "야간은 정상 active"를 기대했으나,
        그건 만기 종목에 대해 틀린 계약이다(만기 지난 종목은 야간 거래가 없다).
        차월물로 넘어간 client가 이 시각에 CM인 것은 아래 next-month 테스트가 잠근다.
        """
        now = datetime(2026, 5, 18, 18, 30, 0)
        self.assertIsNone(get_active_session(now, self.EXPIRY_2026_05))

    # --- contract-aware (PR6c-2d-1 amend, Codex Issue 1 fix) ---

    def test_expiry_day_noon_with_expiring_contract_none(self):
        """5/18 12:00 + contract.expiry_date=5/18 (expiring) → None (11:30 종료 적용)."""
        now = datetime(2026, 5, 18, 12, 0, 0)
        self.assertIsNone(
            get_active_session(now, contract_expiry_date=date(2026, 5, 18))
        )

    def test_expiry_day_noon_with_next_month_active(self):
        """5/18 12:00 + contract.expiry_date=6/15 (next month, A75606 운영) → CF.

        BLOCKING bug fix: PR6c-2d-1이 07:00에 next month로 swap한 후
        만기일 11:30~15:45 동안 disconnect되는 문제 차단.
        """
        now = datetime(2026, 5, 18, 12, 0, 0)
        self.assertEqual(
            get_active_session(now, contract_expiry_date=date(2026, 6, 15)),
            "CF",
        )

    def test_expiry_day_at_15_45_with_next_month_active(self):
        """5/18 15:45 + next month (6/15) → CF (정상 정규장 종료 시각, 포함)."""
        now = datetime(2026, 5, 18, 15, 45, 0)
        self.assertEqual(
            get_active_session(now, contract_expiry_date=date(2026, 6, 15)),
            "CF",
        )

    def test_expiry_day_at_11_30_01_with_expiring_contract(self):
        """5/18 11:30:01 + expiring (5/18) → None (만기 종료 직후)."""
        now = datetime(2026, 5, 18, 11, 30, 1)
        self.assertIsNone(
            get_active_session(now, contract_expiry_date=date(2026, 5, 18))
        )

    def test_expiry_day_at_11_30_with_expiring_contract_active(self):
        """5/18 11:30:00 + expiring (5/18) → CF (종료 시각 inclusive)."""
        now = datetime(2026, 5, 18, 11, 30, 0)
        self.assertEqual(
            get_active_session(now, contract_expiry_date=date(2026, 5, 18)),
            "CF",
        )

    def test_non_expiry_day_with_any_contract(self):
        """5/4 (만기 아님) + 어떤 contract든 → 정규세션 정상 15:45 종료."""
        now = datetime(2026, 5, 4, 14, 0, 0)
        # contract.expiry_date != today 케이스
        self.assertEqual(
            get_active_session(now, contract_expiry_date=date(2026, 5, 18)),
            "CF",
        )

    # (구 `test_legacy_none_contract_uses_calendar` 삭제 — 2026-08-16.
    #  `contract_expiry_date=None` 경로 자체가 제거됐다. None을 넘기면 이제
    #  TypeError이고, 그 계약은 `TestGetActiveSessionRequiresExpiry`가 잠근다.)

    # --- Codex Issue 3 fix: expiring contract 11:30 이후 전체 세션 차단 ---

    def test_expiry_day_night_with_expiring_contract_none(self):
        """5/18 18:30 + expiring (5/18) → None (만기 종목 야간장 없음).

        Codex Issue 3 BLOCKING fix: contract-aware 정규세션 분기만 처리하고
        야간세션 분기를 빠뜨리면 만기 종목이 야간장 active로 잘못 판단됨.
        """
        now = datetime(2026, 5, 18, 18, 30, 0)
        self.assertIsNone(
            get_active_session(now, contract_expiry_date=date(2026, 5, 18))
        )

    def test_expired_contract_returns_none(self):
        """5/19 09:00 + expired (5/18) → None (만기 지난 종목)."""
        now = datetime(2026, 5, 19, 9, 0, 0)
        self.assertIsNone(
            get_active_session(now, contract_expiry_date=date(2026, 5, 18))
        )

    def test_expiry_day_night_with_next_month_active(self):
        """5/18 18:30 + next month (6/15) → CM (정상 야간장)."""
        now = datetime(2026, 5, 18, 18, 30, 0)
        self.assertEqual(
            get_active_session(now, contract_expiry_date=date(2026, 6, 15)),
            "CM",
        )

    def test_expiry_day_dawn_with_expiring_not_blocked_by_amend(self):
        """5/18 05:30 + expiring (5/18) — 11:30 이전이라 expiring 차단 미적용.

        만기일 05:30은 정책상 만기 종목 정상 거래 가능 시간 (전 영업일 시작
        야간장이 이어진 구간). 5/18 케이스는 5/17 일요일 시작 야간장이 없어
        결과적으로 None이지만, 의도는 "11:30 이전은 amend 차단 X"임을 검증.
        """
        now = datetime(2026, 5, 18, 5, 30, 0)
        # 11:30 이전이라 amend의 early return 안 함 → 야간 분기로 들어감
        # 5/17 일요일이라 not business_day → 자연스럽게 None
        self.assertIsNone(
            get_active_session(now, contract_expiry_date=date(2026, 5, 18))
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
