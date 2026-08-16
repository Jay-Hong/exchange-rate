"""app/calendars/krx_calendar.py 단위 테스트 — 정규장/야간장 분리 + 만기 휴장 보정.

외부 의존 0 (holidays 라이브러리 제외). DB/Redis/네트워크 없음.

실행:
    python -m pytest tests/test_krx_calendar.py -p no:asyncio -q
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from app.calendars import krx_calendar as C
from app.calendars.krx_calendar import (
    EXPIRY_WALK_BACK_LIMIT_DAYS,
    is_krx_night_session_open,
    is_krx_regular_business_day,
    third_monday,
    usdf_expiry_date,
)

# holidays==0.97 기준 2020~2030 보정 발생 월 전수 (실측).
# ⚠️ 이 map은 **라이브러리 버전 특성화**다 — holidays 업그레이드로 공휴일
# 지정이 바뀌면 여기가 먼저 깨져야 한다. 만기일은 과거 백필 segment 경계까지
# 결정하므로, 조용한 drift는 이미 적재된 일봉과 새 manifest를 어긋나게 만든다.
EXPECTED_CORRECTIONS = {
    (2020, 8): (date(2020, 8, 17), date(2020, 8, 14)),
    (2021, 8): (date(2021, 8, 16), date(2021, 8, 13)),
    (2021, 9): (date(2021, 9, 20), date(2021, 9, 17)),
    (2022, 8): (date(2022, 8, 15), date(2022, 8, 12)),
    (2024, 9): (date(2024, 9, 16), date(2024, 9, 13)),
    (2026, 2): (date(2026, 2, 16), date(2026, 2, 13)),
    (2026, 8): (date(2026, 8, 17), date(2026, 8, 14)),
    (2027, 8): (date(2027, 8, 16), date(2027, 8, 13)),
    (2028, 7): (date(2028, 7, 17), date(2028, 7, 14)),
    (2029, 5): (date(2029, 5, 21), date(2029, 5, 18)),
}


class TestExpiryProjectionMap(unittest.TestCase):
    """2020~2030 **전체 map**이 기대와 정확히 일치 (11번째 drift까지 검출)."""

    def _actual_corrections(self):
        out = {}
        for year in range(2020, 2031):
            for month in range(1, 13):
                naive = third_monday(year, month)
                corrected = usdf_expiry_date(year, month)
                if naive != corrected:
                    out[(year, month)] = (naive, corrected)
        return out

    def test_correction_map_exact_match(self):
        """보정 발생 월 집합과 각 값이 기대 map과 **완전 일치**.

        알려진 10건만 개별 확인하면 새로 생기는 11번째를 놓친다. 집합 전체를
        비교해야 추가·변경·삭제가 모두 걸린다.
        """
        self.assertEqual(self._actual_corrections(), EXPECTED_CORRECTIONS)

    def test_all_corrections_are_three_days(self):
        """실측상 보정폭은 전부 3일(월요일 휴장 → 주말 → 직전 금요일).

        walk-back 한도 14일이 4배 이상 여유라는 근거이자, 비정상적으로 큰
        보정이 생기면 알아채는 축.
        """
        for key, (naive, corrected) in self._actual_corrections().items():
            with self.subTest(key=key):
                self.assertEqual((naive - corrected).days, 3)

    def test_corrected_expiry_is_always_regular_business_day(self):
        for year in range(2020, 2031):
            for month in range(1, 13):
                with self.subTest(year=year, month=month):
                    self.assertTrue(
                        is_krx_regular_business_day(usdf_expiry_date(year, month))
                    )


class TestExpiryBoundaries(unittest.TestCase):
    def test_2026_08_liberation_day_substitute(self):
        """8/17 = 광복절(8/15 토) 대체공휴일 → 8/14(금). 2026-08-14 사고의 근인."""
        self.assertEqual(third_monday(2026, 8), date(2026, 8, 17))
        self.assertEqual(usdf_expiry_date(2026, 8), date(2026, 8, 14))

    def test_2026_02_lunar_new_year(self):
        """2/16 = 설 연휴 → 주말 2일 건너 2/13(금). 단일 스텝이 아닌 루프 필요."""
        self.assertEqual(third_monday(2026, 2), date(2026, 2, 16))
        self.assertEqual(usdf_expiry_date(2026, 2), date(2026, 2, 13))

    def test_business_day_third_monday_unchanged(self):
        """셋째 월요일이 영업일이면 보정 0회 — **과보정 회귀 방어**."""
        for year, month, expected in [
            (2026, 5, date(2026, 5, 18)),
            (2026, 6, date(2026, 6, 15)),
            (2026, 7, date(2026, 7, 20)),
            (2026, 12, date(2026, 12, 21)),  # 연말 폐장(12/31)과 무간섭
        ]:
            with self.subTest(year=year, month=month):
                self.assertEqual(usdf_expiry_date(year, month), expected)

    def test_walk_back_limit_raises_instead_of_silent_wrong_date(self):
        """한도 초과 시 **조용히 틀린 날짜를 반환하지 않고** 실패한다 (Crash Early)."""
        with patch.object(C, "is_krx_regular_business_day", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                usdf_expiry_date(2026, 8)
        self.assertIn(str(EXPIRY_WALK_BACK_LIMIT_DAYS), str(ctx.exception))

    def test_walk_back_exact_boundary_0_3_14_ok_15_raises(self):
        """순수 stub으로 보정폭 경계를 정확히 잠근다 (off-by-one 방어).

        "영업일 없음"만 검증하면 한도가 13이든 14든 15든 다 통과한다.
        `EXPIRY_WALK_BACK_LIMIT_DAYS`(=14)일 보정은 **성공**, 15일은 **실패**여야
        한다 — 그 경계 자체를 잠근다 (codex 리뷰 지적).
        """
        naive = third_monday(2026, 8)  # 2026-08-17

        def stub_only(target: date):
            return lambda d: d == target

        for back in (0, 3, EXPIRY_WALK_BACK_LIMIT_DAYS):
            target = naive - timedelta(days=back)
            with self.subTest(back=back):
                with patch.object(C, "is_krx_regular_business_day", stub_only(target)):
                    self.assertEqual(usdf_expiry_date(2026, 8), target)

        too_far = naive - timedelta(days=EXPIRY_WALK_BACK_LIMIT_DAYS + 1)
        with patch.object(C, "is_krx_regular_business_day", stub_only(too_far)):
            with self.assertRaises(RuntimeError):
                usdf_expiry_date(2026, 8)


class TestRegularBusinessDay(unittest.TestCase):
    def test_weekend_closed(self):
        self.assertFalse(is_krx_regular_business_day(date(2026, 8, 15)))  # 토
        self.assertFalse(is_krx_regular_business_day(date(2026, 8, 16)))  # 일

    def test_substitute_holiday_closed(self):
        """2026-08-17 = 광복절 대체공휴일. 동적 캘린더(observed=True)가 커버."""
        self.assertFalse(is_krx_regular_business_day(date(2026, 8, 17)))

    def test_normal_weekday_open(self):
        self.assertTrue(is_krx_regular_business_day(date(2026, 8, 14)))
        self.assertTrue(is_krx_regular_business_day(date(2026, 8, 18)))

    def test_year_end_closure(self):
        self.assertFalse(is_krx_regular_business_day(date(2026, 12, 31)))
        self.assertTrue(is_krx_regular_business_day(date(2026, 12, 30)))


class TestNightSessionOpen(unittest.TestCase):
    """야간장은 **시작일 기준** + 야간 전용 override."""

    def test_defaults_to_regular_calendar(self):
        self.assertTrue(is_krx_night_session_open(date(2026, 8, 14)))   # 금
        self.assertFalse(is_krx_night_session_open(date(2026, 8, 15)))  # 토
        self.assertFalse(is_krx_night_session_open(date(2026, 8, 17)))  # 대체공휴일

    def test_force_closed_blocks_open_regular_day(self):
        """정규장은 열렸으나 야간장만 휴장하는 공지 케이스."""
        with patch.object(
            C, "_KRX_NIGHT_SESSION_FORCE_CLOSED", frozenset({date(2026, 2, 13)})
        ):
            self.assertTrue(is_krx_regular_business_day(date(2026, 2, 13)))
            self.assertFalse(is_krx_night_session_open(date(2026, 2, 13)))

    def test_force_open_allows_non_business_day(self):
        with patch.object(
            C, "_KRX_NIGHT_SESSION_FORCE_OPEN", frozenset({date(2026, 8, 15)})
        ):
            self.assertFalse(is_krx_regular_business_day(date(2026, 8, 15)))
            self.assertTrue(is_krx_night_session_open(date(2026, 8, 15)))

    def test_override_conflict_raises_instead_of_silent_open(self):
        """양쪽 테이블 교집합은 **데이터 오류** → RuntimeError.

        둘 다 공식 공지 기반이므로 우선순위로 해소할 사안이 아니다. 구 구현은
        force_open을 택했는데, 그러면 잘못된 등재가 조용히 개장으로 뭉개져
        드러나지 않는다 (codex 리뷰 지적).
        """
        d = date(2026, 8, 14)
        with patch.object(C, "_KRX_NIGHT_SESSION_FORCE_OPEN", frozenset({d})), \
             patch.object(C, "_KRX_NIGHT_SESSION_FORCE_CLOSED", frozenset({d})):
            with self.assertRaises(RuntimeError) as ctx:
                is_krx_night_session_open(d)
        self.assertIn("충돌", str(ctx.exception))

    def test_override_conflict_only_on_intersecting_date(self):
        """교집합이 아닌 날짜는 정상 판정 — 예외가 테이블 전체를 막지 않는다."""
        conflict, other = date(2026, 8, 14), date(2026, 8, 21)
        with patch.object(C, "_KRX_NIGHT_SESSION_FORCE_OPEN", frozenset({conflict})), \
             patch.object(C, "_KRX_NIGHT_SESSION_FORCE_CLOSED", frozenset({conflict, other})):
            self.assertFalse(is_krx_night_session_open(other))

    def test_registered_override_content_is_exact(self):
        """등재 내용을 정확히 잠근다 — 공시 확인된 날짜만, 그 이상도 이하도 아니게.

        `force_closed`는 2026-02-13 **단건**(KIND acptno=20260206002546, 2026.02.06
        "야간파생상품시장 공지" 원문 확인). `force_open`은 알려진 사례가 없어 비어 있다.
        미검증 날짜가 추가되면(추측 기반 캘린더 = 2026-05-25 사고의 역방향 재연)
        이 테스트가 먼저 깨진다.
        """
        self.assertEqual(
            C._KRX_NIGHT_SESSION_FORCE_CLOSED, frozenset({date(2026, 2, 13)})
        )
        self.assertEqual(C._KRX_NIGHT_SESSION_FORCE_OPEN, frozenset())

    def test_2026_02_13_night_closed_by_official_notice(self):
        """2026-02-13 시작 야간장 휴장 — 정규장은 정상 개장한 날이다.

        KRX 공지: '26.2.13(금) 18시 ~ '26.2.14(토) 06시 야간거래 全상품 휴장
        (미국달러선물 명시). 사유는 장기연휴(설 2/16~18) 리스크관리 부담 완화.
        """
        self.assertTrue(is_krx_regular_business_day(date(2026, 2, 13)))
        self.assertFalse(is_krx_night_session_open(date(2026, 2, 13)))

    def test_adjacent_nights_unaffected(self):
        """인접 야간장은 영향 없음 — 단건 override가 범위를 넘지 않는다."""
        self.assertTrue(is_krx_night_session_open(date(2026, 2, 12)))  # 목
        # 2/16~18은 설 연휴라 정규장 기준으로 이미 닫혀 있다 (override 무관)
        self.assertFalse(is_krx_night_session_open(date(2026, 2, 16)))


class TestRegisteredClosureEndToEnd(unittest.TestCase):
    """등재된 2026-02-13 휴장이 **기본 동작으로** 세션·close 판정까지 전파되는가.

    patch 없이 실제 테이블을 쓴다 — override가 등재만 되고 배선이 끊겨 있으면
    여기서 잡힌다.
    """

    # 2/13 시점의 운영 계약은 3월물(A75603). 2월물은 그날이 만기라 별도 축이므로
    # 세션 판정 테스트에는 차월물 만기를 쓴다(만기 분기와 야간 override 분리).
    MAR_EXPIRY = usdf_expiry_date(2026, 3)

    def test_next_month_contract_has_no_session_on_closed_night(self):
        from app.sources.kis_futures import get_active_session

        self.assertIsNone(
            get_active_session(datetime(2026, 2, 13, 18, 0), self.MAR_EXPIRY)
        )
        self.assertIsNone(
            get_active_session(datetime(2026, 2, 13, 23, 30), self.MAR_EXPIRY)
        )

    def test_next_dawn_also_closed_via_start_date(self):
        """2/14 03:00은 시작일이 2/13이므로 동일하게 닫힌다 (시작일 기준 정책)."""
        from app.sources.kis_futures import get_active_session

        self.assertIsNone(
            get_active_session(datetime(2026, 2, 14, 3, 0), self.MAR_EXPIRY)
        )

    def test_cm_close_snapshot_disabled_for_that_night(self):
        """그 밤의 CM close(2/14 06:00 boundary) snapshot도 비활성."""
        from app.sources.kis_futures import is_close_snapshot_eligible

        self.assertFalse(is_close_snapshot_eligible("CM", date(2026, 2, 14)))

    def test_regular_session_that_day_still_active(self):
        """정규장은 정상 — 야간 override가 주간까지 끄지 않는다."""
        from app.sources.kis_futures import get_active_session

        self.assertEqual(
            get_active_session(datetime(2026, 2, 13, 14, 0), self.MAR_EXPIRY), "CF"
        )
        self.assertTrue(is_close_snapshot_eligible_cf())

    def test_february_expiry_still_02_13(self):
        """2월 만기는 **정규장 기준** 2/13 유지 — 야간 휴장이 만기를 당기지 않는다."""
        self.assertEqual(usdf_expiry_date(2026, 2), date(2026, 2, 13))


def is_close_snapshot_eligible_cf() -> bool:
    from app.sources.kis_futures import is_close_snapshot_eligible

    return is_close_snapshot_eligible("CF", date(2026, 2, 13))


class TestNightOverrideDoesNotLeakIntoExpiry(unittest.TestCase):
    """⭐ 야간 override가 **만기 walk-back으로 새지 않는다**.

    새면 2026-02-13처럼 정규장이 정상 개장한 날을 휴장으로 오판해 만기를
    2/12로 하루 더 당긴다. 정규장/야간장 분리가 설계 원칙이 아니라
    **테스트로 강제되는 불변식**임을 잠그는 축이다.
    """

    def test_expiry_unaffected_by_night_force_closed(self):
        with patch.object(
            C, "_KRX_NIGHT_SESSION_FORCE_CLOSED", frozenset({date(2026, 2, 13)})
        ):
            self.assertFalse(is_krx_night_session_open(date(2026, 2, 13)))
            # 만기는 정규장 캘린더만 보므로 2/13 그대로 (2/12로 당겨지지 않음)
            self.assertEqual(usdf_expiry_date(2026, 2), date(2026, 2, 13))

    def test_expiry_unaffected_by_night_force_open(self):
        with patch.object(
            C, "_KRX_NIGHT_SESSION_FORCE_OPEN", frozenset({date(2026, 8, 17)})
        ):
            self.assertTrue(is_krx_night_session_open(date(2026, 8, 17)))
            # 야간이 열려도 정규장은 휴장이므로 만기는 8/14 유지
            self.assertEqual(usdf_expiry_date(2026, 8), date(2026, 8, 14))


if __name__ == "__main__":
    unittest.main()
