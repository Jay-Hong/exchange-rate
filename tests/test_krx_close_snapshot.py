"""KRX close snapshot tests — KRX_CLOSE_SNAPSHOT_PLAN.md 1차 PR.

CF 15:45 / CM 06:00 단일가 종가 누락 보강.

검증 영역:
    - boundary helper (compute_close_boundary_kst / is_close_snapshot_eligible)
    - insert_source_rate_if_changed timestamp 인자 (backward compat + 명시 전달)
    - KrxCloseSnapshotController 동작 (schedule / retry / sanity / DB+Redis)
    - KisFuturesClient session change → schedule 통합
    - 격리: REST 실패 / sanity abort / 휴장일 / 예외
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.crawlers.krx_kis import (
    KrxCloseSnapshotController,
)
from app.sources.kis_futures import (
    KST,
    compute_close_boundary_kst,
    is_close_snapshot_eligible,
)
from app.sources.kis_master import ContractInfo


def _make_contract() -> ContractInfo:
    return ContractInfo(
        short_code="A75605",
        standard_code="KR4A75650007",
        name="미국달러 F 202605",
        contract_month="202605",
        expiry_date=date(2026, 5, 18),
    )


def _make_rest_result(price: str = "1500.2") -> dict:
    return {
        "source": "krx",
        "asset": "usd-krw-futures",
        "contract_code": "A75605",
        "contract_month": "202605",
        "expires_on": "2026-05-18",
        "price": price,
        "session": "CF",
        "market_div_code": "CF",
        "received_at": "2026-05-15T15:46:00+09:00",
        "raw_rt_cd": "0",
    }


# ---------------------------------------------------------------------------
# boundary helpers (sources/kis_futures.py)
# ---------------------------------------------------------------------------

class TestBoundaryHelpers(unittest.TestCase):

    def test_compute_close_boundary_cf(self):
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))
        self.assertEqual(boundary, datetime(2026, 5, 15, 15, 45, 0, 0, tzinfo=KST))
        self.assertEqual(boundary.microsecond, 0)

    def test_compute_close_boundary_cm(self):
        boundary = compute_close_boundary_kst("CM", date(2026, 5, 16))
        self.assertEqual(boundary, datetime(2026, 5, 16, 6, 0, 0, 0, tzinfo=KST))

    def test_compute_close_boundary_invalid_session(self):
        with self.assertRaises(ValueError):
            compute_close_boundary_kst("XX", date(2026, 5, 15))  # type: ignore[arg-type]

    def test_eligible_cf_business_day(self):
        # 2026-05-15 금요일 — 정규 영업일
        with patch("app.sources.kis_futures.is_krx_business_day", return_value=True):
            self.assertTrue(is_close_snapshot_eligible("CF", date(2026, 5, 15)))

    def test_eligible_cf_holiday(self):
        with patch("app.sources.kis_futures.is_krx_business_day", return_value=False):
            self.assertFalse(is_close_snapshot_eligible("CF", date(2026, 5, 16)))

    def test_eligible_cm_saturday_uses_previous_friday(self):
        """토요일 06:00 = 금요일 야간장 종료 → today=Sat, check=Fri."""
        calls = []

        def fake_business_day(d: date) -> bool:
            calls.append(d)
            return d == date(2026, 5, 15)  # Friday

        with patch("app.sources.kis_futures.is_krx_business_day",
                   side_effect=fake_business_day):
            self.assertTrue(is_close_snapshot_eligible("CM", date(2026, 5, 16)))
        # CM check는 today - 1 day = Friday
        self.assertEqual(calls, [date(2026, 5, 15)])

    def test_eligible_cf_2026_05_25_buddhas_birthday_substitute_skips(self):
        """5/25 월요일 부처님오신날 대체공휴일 — CF skip (운영 사고 회귀 잠금).

        mock 없이 실제 KRX_2026_KNOWN_HOLIDAYS 캘린더 entry 검증 — 캘린더에서
        5/25 빠지면 즉시 fail.
        """
        self.assertFalse(is_close_snapshot_eligible("CF", date(2026, 5, 25)))

    def test_eligible_cm_2026_05_26_after_buddhas_birthday_substitute_skips(self):
        """5/26 화요일 06:00 KST CM — today-1=5/25 휴일 야간장 시작일이라 skip.

        CM의 calendar day(today) vs business day check(today-1) 분리 정책 잠금.
        mock 없이 실제 캘린더 entry 검증 — 5/25 휴일 entry가 5/26 06:00 CM도
        자연 차단.
        """
        self.assertFalse(is_close_snapshot_eligible("CM", date(2026, 5, 26)))


# ---------------------------------------------------------------------------
# crud.insert_source_rate_if_changed timestamp 인자 (backward compat)
# ---------------------------------------------------------------------------

class TestInsertSourceRateTimestamp(unittest.TestCase):

    def test_backward_compat_no_timestamp(self):
        """timestamp 미전달 시 기존 동작 (DB DEFAULT 사용)."""
        from app import crud, models

        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        captured = {}

        def add_capture(record):
            captured["record"] = record

        db.add = MagicMock(side_effect=add_capture)
        db.commit = MagicMock()

        result = crud.insert_source_rate_if_changed(
            db, source="krx", asset="usd-krw-futures", rate=1500.0,
        )
        self.assertTrue(result)
        # timestamp 인자 전달 안 함 — record.timestamp는 SourceRate default 적용 (None 또는 자동값)
        # SQLAlchemy default는 INSERT 시점에 적용되므로 record.timestamp는 None일 수 있음

    def test_explicit_timestamp(self):
        """timestamp 명시 전달 시 record.timestamp에 set."""
        from app import crud, models

        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        captured = {}

        def add_capture(record):
            captured["record"] = record

        db.add = MagicMock(side_effect=add_capture)
        db.commit = MagicMock()

        explicit_ts = datetime(2026, 5, 15, 6, 45, 0)  # UTC naive (CF 15:45 KST)
        result = crud.insert_source_rate_if_changed(
            db, source="krx", asset="usd-krw-futures", rate=1500.0,
            timestamp=explicit_ts,
        )
        self.assertTrue(result)
        self.assertEqual(captured["record"].timestamp, explicit_ts)

    def test_dedup_skip_same_rate(self):
        """가격 동일 시 INSERT 안 함 (timestamp 무관)."""
        from app import crud

        last = MagicMock()
        last.rate = 1500.0
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = last

        result = crud.insert_source_rate_if_changed(
            db, source="krx", asset="usd-krw-futures", rate=1500.0,
            timestamp=datetime(2026, 5, 15, 6, 45, 0),
        )
        self.assertFalse(result)
        db.add.assert_not_called()


# ---------------------------------------------------------------------------
# KrxCloseSnapshotController — schedule + retry + REST + sanity + DB/Redis
# ---------------------------------------------------------------------------

class TestKrxCloseSnapshotController(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        """기존 1차 PR (c0855ff) 동작 검증 tests — env=false patch 유지.

        Stage 4 (2026-05-17): KRX_CLOSE_FINALIZER_ENABLED=true default가
        _retry_sequence를 1회 fallback + captured flag GET 분기로 격하시키므로
        1차 PR 시그니처 검증용 기존 tests는 env=false로 격리.
        Stage 4 정책은 test_krx_close_snapshot_controller_fallback.py에서 별도 검증.
        """
        from app import config
        self._env_patcher = patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", False)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    def _make_controller(
        self,
        retry_delays_sec=None,
    ) -> KrxCloseSnapshotController:
        token_manager = MagicMock()
        return KrxCloseSnapshotController(
            token_manager=token_manager,
            # 빠른 테스트 — 모든 retry 즉시 진행
            retry_delays_sec=retry_delays_sec or {"CF": [0.0, 0.0, 0.0], "CM": [0.0, 0.0, 0.0]},
            sanity_pct=0.02,
            rest_timeout_sec=1.0,
        )

    async def test_first_attempt_success_short_circuits(self):
        """첫 retry 성공 → 남은 retry skip."""
        controller = self._make_controller()
        contract = _make_contract()
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))

        with patch(
            "app.crawlers.krx_kis.fetch_kis_futures_quote",
            new=AsyncMock(return_value=_make_rest_result("1500.2")),
        ) as mock_fetch, patch(
            "app.crud.get_latest_source_rate",
            return_value={"rate": 1500.9, "timestamp": "..."},
        ), patch(
            "app.crud.insert_source_rate_if_changed", return_value=True,
        ) as mock_insert, patch(
            "app.latest_rates_cache.set_latest_krx_rate_from_sync_job",
            return_value=True,
        ) as mock_redis, patch(
            "app.database.get_db_context",
        ):
            controller.schedule_close_snapshot(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )
            await asyncio.sleep(0.1)
            await controller.close(timeout=1.0)

        self.assertEqual(mock_fetch.await_count, 1)  # 1번만 호출 (첫 성공)
        mock_insert.assert_called_once()
        mock_redis.assert_called_once()
        # Redis call에 boundary KST ISO 전달 확인
        redis_kwargs = mock_redis.call_args.kwargs
        self.assertEqual(redis_kwargs["timestamp"], "2026-05-15T15:45:00+09:00")
        self.assertEqual(controller.counters["success"], 1)

    async def test_sanity_check_aborts_high_diff(self):
        """±2% 초과 차이 → abort + 다음 retry."""
        controller = self._make_controller()
        contract = _make_contract()
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))

        # last_rate=1500.0, REST=1600.0 → diff 6.67% > 2% → abort
        with patch(
            "app.crawlers.krx_kis.fetch_kis_futures_quote",
            new=AsyncMock(return_value=_make_rest_result("1600.0")),
        ) as mock_fetch, patch(
            "app.crud.get_latest_source_rate",
            return_value={"rate": 1500.0, "timestamp": "..."},
        ), patch(
            "app.crud.insert_source_rate_if_changed",
        ) as mock_insert, patch(
            "app.latest_rates_cache.set_latest_krx_rate_from_sync_job",
        ) as mock_redis, patch(
            "app.database.get_db_context",
        ):
            controller.schedule_close_snapshot(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )
            await asyncio.sleep(0.1)
            await controller.close(timeout=1.0)

        # 3 retry 모두 sanity abort → INSERT/Redis 호출 0
        self.assertEqual(mock_fetch.await_count, 3)
        mock_insert.assert_not_called()
        mock_redis.assert_not_called()
        self.assertEqual(controller.counters["sanity_aborted"], 3)
        self.assertEqual(controller.counters["exhausted"], 1)

    async def test_rest_failure_retries(self):
        """REST None 반환 → retry 진행."""
        controller = self._make_controller()
        contract = _make_contract()
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))

        # 첫 2번 None, 3번째 성공
        fetch_results = [None, None, _make_rest_result("1500.2")]
        fetch_mock = AsyncMock(side_effect=fetch_results)

        with patch(
            "app.crawlers.krx_kis.fetch_kis_futures_quote", new=fetch_mock,
        ), patch(
            "app.crud.get_latest_source_rate",
            return_value={"rate": 1500.0, "timestamp": "..."},
        ), patch(
            "app.crud.insert_source_rate_if_changed", return_value=True,
        ) as mock_insert, patch(
            "app.latest_rates_cache.set_latest_krx_rate_from_sync_job",
            return_value=True,
        ), patch(
            "app.database.get_db_context",
        ):
            controller.schedule_close_snapshot(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )
            await asyncio.sleep(0.1)
            await controller.close(timeout=1.0)

        self.assertEqual(fetch_mock.await_count, 3)
        mock_insert.assert_called_once()
        self.assertEqual(controller.counters["success"], 1)
        self.assertEqual(controller.counters["rest_failed"], 2)

    async def test_rest_exception_isolated(self):
        """REST 예외 → retry 진행, controller 격리."""
        controller = self._make_controller()
        contract = _make_contract()
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))

        with patch(
            "app.crawlers.krx_kis.fetch_kis_futures_quote",
            new=AsyncMock(side_effect=RuntimeError("REST boom")),
        ) as mock_fetch:
            controller.schedule_close_snapshot(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )
            await asyncio.sleep(0.1)
            await controller.close(timeout=1.0)

        # 3 retry 모두 예외 — exhausted
        self.assertEqual(mock_fetch.await_count, 3)
        self.assertEqual(controller.counters["rest_failed"], 3)
        self.assertEqual(controller.counters["exhausted"], 1)

    async def test_db_redis_write_uses_boundary_timestamp(self):
        """DB UTC naive + Redis KST ISO 둘 다 boundary 사용."""
        controller = self._make_controller()
        contract = _make_contract()
        # CF 15:45 KST = UTC 06:45
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))
        expected_utc_naive = datetime(2026, 5, 15, 6, 45, 0)
        expected_kst_iso = "2026-05-15T15:45:00+09:00"

        with patch(
            "app.crawlers.krx_kis.fetch_kis_futures_quote",
            new=AsyncMock(return_value=_make_rest_result("1500.2")),
        ), patch(
            "app.crud.get_latest_source_rate", return_value=None,
        ), patch(
            "app.crud.insert_source_rate_if_changed", return_value=True,
        ) as mock_insert, patch(
            "app.latest_rates_cache.set_latest_krx_rate_from_sync_job",
            return_value=True,
        ) as mock_redis, patch(
            "app.database.get_db_context",
        ):
            controller.schedule_close_snapshot(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )
            await asyncio.sleep(0.1)
            await controller.close(timeout=1.0)

        insert_kwargs = mock_insert.call_args.kwargs
        self.assertEqual(insert_kwargs["timestamp"], expected_utc_naive)
        self.assertEqual(insert_kwargs["timestamp"].tzinfo, None)  # naive 확인
        redis_kwargs = mock_redis.call_args.kwargs
        self.assertEqual(redis_kwargs["timestamp"], expected_kst_iso)

    async def test_redis_set_failure_triggers_next_retry(self):
        """Finding 1 회귀 가드 — Redis SET False → attempt False → 다음 retry 진행.

        scenario: 첫 retry Redis False, 두 번째 retry True → 최종 success 1회.
        """
        controller = self._make_controller()
        contract = _make_contract()
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))

        # Redis 호출: 첫 번째 False, 두 번째 True
        redis_results = [False, True]
        redis_mock = MagicMock(side_effect=redis_results)

        with patch(
            "app.crawlers.krx_kis.fetch_kis_futures_quote",
            new=AsyncMock(return_value=_make_rest_result("1500.2")),
        ) as mock_fetch, patch(
            "app.crud.get_latest_source_rate",
            return_value={"rate": 1500.0, "timestamp": "..."},
        ), patch(
            "app.crud.insert_source_rate_if_changed", return_value=True,
        ), patch(
            "app.latest_rates_cache.set_latest_krx_rate_from_sync_job",
            new=redis_mock,
        ), patch(
            "app.database.get_db_context",
        ):
            controller.schedule_close_snapshot(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )
            await asyncio.sleep(0.1)
            await controller.close(timeout=1.0)

        # 첫 REST 호출 → Redis False → retry → 두 번째 REST 호출 → Redis True → success
        self.assertEqual(mock_fetch.await_count, 2)
        self.assertEqual(redis_mock.call_count, 2)
        self.assertEqual(controller.counters["success"], 1)
        # 첫 attempt가 false 처리되었음 (success는 두 번째에서만 +1)
        self.assertEqual(controller.counters["attempted"], 2)

    async def test_close_cancels_pending(self):
        """close timeout 시 pending task cancel."""
        # 매우 긴 delay로 task hang
        controller = self._make_controller(
            retry_delays_sec={"CF": [10.0, 20.0, 30.0]}
        )
        contract = _make_contract()
        # boundary를 미래로 설정 → wait_sec > 0 → asyncio.sleep으로 hang → cancel 검증
        future_boundary = datetime.now(KST).replace(microsecond=0) + timedelta(hours=1)

        with patch(
            "app.crawlers.krx_kis.fetch_kis_futures_quote",
            new=AsyncMock(return_value=_make_rest_result("1500.2")),
        ) as mock_fetch:
            controller.schedule_close_snapshot(
                contract=contract, session="CF", boundary_at_kst=future_boundary,
            )
            # close timeout 짧게 → task cancel (asyncio.sleep 중간에)
            await controller.close(timeout=0.05)

        # REST 호출 안 됨 (sleep 중 cancel)
        mock_fetch.assert_not_called()
        self.assertEqual(len(controller._tasks), 0)


# ---------------------------------------------------------------------------
# KisFuturesClient._maybe_schedule_close_snapshot integration
# ---------------------------------------------------------------------------

class TestKisFuturesClientSchedulesCloseSnapshot(unittest.TestCase):

    def _make_client_with_controller(self):
        from app.crawlers.krx_kis import (
            KisApprovalManager, KisAccessTokenManager, KisFuturesClient,
        )
        approval = MagicMock(spec=KisApprovalManager)
        token_mgr = MagicMock(spec=KisAccessTokenManager)
        return KisFuturesClient(
            approval_manager=approval,
            contract=_make_contract(),
            access_token_manager=token_mgr,
        )

    def test_cf_session_change_schedules_snapshot(self):
        client = self._make_client_with_controller()
        with patch.object(
            client._close_snapshot_controller,
            "schedule_close_snapshot",
        ) as mock_schedule, patch(
            "app.crawlers.krx_kis.is_close_snapshot_eligible",
            return_value=True,
        ):
            client._maybe_schedule_close_snapshot(
                ended_session="CF", today_kst=date(2026, 5, 15),
            )
        mock_schedule.assert_called_once()
        kwargs = mock_schedule.call_args.kwargs
        self.assertEqual(kwargs["session"], "CF")
        self.assertEqual(
            kwargs["boundary_at_kst"],
            datetime(2026, 5, 15, 15, 45, 0, 0, tzinfo=KST),
        )

    def test_holiday_skip(self):
        client = self._make_client_with_controller()
        with patch.object(
            client._close_snapshot_controller,
            "schedule_close_snapshot",
        ) as mock_schedule, patch(
            "app.crawlers.krx_kis.is_close_snapshot_eligible",
            return_value=False,
        ):
            client._maybe_schedule_close_snapshot(
                ended_session="CF", today_kst=date(2026, 5, 16),
            )
        mock_schedule.assert_not_called()

    def test_controller_none_when_no_token_manager(self):
        from app.crawlers.krx_kis import KisApprovalManager, KisFuturesClient
        approval = MagicMock(spec=KisApprovalManager)
        client = KisFuturesClient(
            approval_manager=approval,
            contract=_make_contract(),
            access_token_manager=None,
        )
        # controller None — schedule skip safely
        client._maybe_schedule_close_snapshot(
            ended_session="CF", today_kst=date(2026, 5, 15),
        )
        # 예외 없이 silent skip 확인
        self.assertIsNone(client._close_snapshot_controller)

    def test_invalid_session_ignored(self):
        client = self._make_client_with_controller()
        with patch.object(
            client._close_snapshot_controller,
            "schedule_close_snapshot",
        ) as mock_schedule:
            client._maybe_schedule_close_snapshot(
                ended_session="XX", today_kst=date(2026, 5, 15),
            )
        mock_schedule.assert_not_called()

    def test_schedule_exception_isolated(self):
        """controller schedule 예외 → _maybe_schedule_close_snapshot 격리."""
        client = self._make_client_with_controller()
        with patch.object(
            client._close_snapshot_controller,
            "schedule_close_snapshot",
            side_effect=RuntimeError("boom"),
        ), patch(
            "app.crawlers.krx_kis.is_close_snapshot_eligible",
            return_value=True,
        ):
            # 예외 없이 격리 (logger.exception만)
            client._maybe_schedule_close_snapshot(
                ended_session="CF", today_kst=date(2026, 5, 15),
            )

    def test_expiring_cf_today_equals_expiry_skips(self):
        """Finding 2 회귀 가드 — 만기일 CF (today == expiry) → schedule skip.

        rollover 실패로 expiring contract 잔존 시 15:45 boundary 잘못 schedule
        방지 (plan §4.10 scope 제외).
        """
        client = self._make_client_with_controller()
        # contract.expiry_date = 2026-05-18, today도 같은 날짜
        expiry_day = client._contract.expiry_date
        self.assertEqual(expiry_day, date(2026, 5, 18))

        with patch.object(
            client._close_snapshot_controller,
            "schedule_close_snapshot",
        ) as mock_schedule:
            client._maybe_schedule_close_snapshot(
                ended_session="CF", today_kst=expiry_day,
            )
        mock_schedule.assert_not_called()

    def test_expiring_cm_today_equals_expiry_still_schedules(self):
        """Finding 2 회귀 가드 — 만기일 CM (today == expiry) → 정상 schedule.

        CM 06:00 시점에 07:00 swap 전이라 expiring contract 정상 처리 대상.
        만기일 skip 조건을 CF에만 적용해야 — CM 너무 넓게 막으면 안 됨.
        """
        client = self._make_client_with_controller()
        expiry_day = client._contract.expiry_date

        with patch.object(
            client._close_snapshot_controller,
            "schedule_close_snapshot",
        ) as mock_schedule, patch(
            "app.crawlers.krx_kis.is_close_snapshot_eligible",
            return_value=True,
        ):
            client._maybe_schedule_close_snapshot(
                ended_session="CM", today_kst=expiry_day,
            )
        # CM은 만기일에도 정상 schedule
        mock_schedule.assert_called_once()
        kwargs = mock_schedule.call_args.kwargs
        self.assertEqual(kwargs["session"], "CM")
        self.assertEqual(
            kwargs["boundary_at_kst"],
            datetime(expiry_day.year, expiry_day.month, expiry_day.day,
                     6, 0, 0, 0, tzinfo=KST),
        )
