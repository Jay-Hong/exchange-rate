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
        # 2026-06-10 #4 gate chain — gate 2(contract identity)용 resp_* 응답 원본
        # 필드 (_make_contract와 일치하는 gate-passing 기본값).
        "resp_hts_kor_isnm": "미국달러 F 202605",
        "resp_futs_last_tr_date": "20260518",
        "resp_acml_vol": "956734",
    }


def _recent_last(rate: float = 1500.9) -> dict:
    """gate 3(session evidence) 통과용 last mock — boundary 5/15 15:45 기준
    10분 전 fresh tick (구 "..." placeholder는 gate 3 도입으로 reject됨)."""
    return {"rate": rate, "timestamp": "2026-05-15T15:35:00+09:00"}


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

        mock 없이 실제 kr_holidays(+ KRX wrapper) 캘린더 검증 — is_krx_business_day가
        5/25 대체공휴일을 휴장으로 잡지 못하면 즉시 fail.
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

        Policy PR (2026-05-25 사고 대응): KRX_CLOSE_REST_WRITE_ENABLED default
        false로 추가됨. 본 클래스는 1차 PR "기존 write 동작" 검증용이므로 본 flag를
        True로 patch하여 legacy behavior 검증을 유지. flag false 분기 검증은
        TestKrxCloseSnapshotControllerRestWriteBlocked에서 별도 수행.
        """
        from app import config
        self._env_patcher = patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", False)
        self._env_patcher.start()
        self._rest_write_patcher = patch.object(config, "KRX_CLOSE_REST_WRITE_ENABLED", True)
        self._rest_write_patcher.start()

    def tearDown(self):
        self._rest_write_patcher.stop()
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
            return_value=_recent_last(1500.9),
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
            return_value=_recent_last(1500.0),
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
            return_value=_recent_last(1500.0),
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
            # 2026-06-10 #4: 구 return_value=None은 gate 3(last=None → reject)으로
            # 더 이상 write에 도달 못함 (구멍 폐쇄) — fresh last로 교체.
            # None reject 검증은 TestKrxCloseSnapshotControllerGateChain에서.
            "app.crud.get_latest_source_rate", return_value=_recent_last(1500.0),
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
            return_value=_recent_last(1500.0),
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


class TestKrxCloseSnapshotControllerRestWriteBlocked(unittest.IsolatedAsyncioTestCase):
    """KRX_CLOSE_REST_WRITE_ENABLED=false 분기 검증 (2026-05-25 사고 대응).

    KIS REST가 stale 응답을 정상 형식으로 반환할 수 있으므로 REST 기반 DB/Redis
    write를 default off로 격하. REST fetch + sanity는 diagnostic으로 유지하되
    DB/Redis write만 차단 + retry short-circuit (같은 stale 값 3회 확인 무의미).

    finalizer enabled 여부와 무관하게 KrxCloseSnapshotController의 REST write
    전체 차단 — finalizer=true 경로의 fallback 1회, finalizer=false rollback
    경로의 retry 3회 모두 영향.
    """

    def _make_controller(self) -> KrxCloseSnapshotController:
        token_manager = MagicMock()
        return KrxCloseSnapshotController(
            token_manager=token_manager,
            retry_delays_sec={"CF": [0.0, 0.0, 0.0], "CM": [0.0, 0.0, 0.0]},
            sanity_pct=0.02,
            rest_timeout_sec=1.0,
        )

    async def test_rest_write_blocked_finalizer_enabled(self):
        """finalizer=true + captured=false + REST_WRITE_ENABLED=false → write 미호출 + retry short-circuit."""
        from app import config
        controller = self._make_controller()
        contract = _make_contract()
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch.object(config, "KRX_CLOSE_REST_WRITE_ENABLED", False), \
             patch(
                 "app.latest_rates_cache.get_krx_close_captured_flag",
                 return_value=False,  # WS 미캡처 — REST 진행
             ), patch(
                 "app.crawlers.krx_kis.fetch_kis_futures_quote",
                 new=AsyncMock(return_value=_make_rest_result("1500.2")),
             ) as mock_fetch, patch(
                 "app.crud.get_latest_source_rate",
                 return_value=_recent_last(1500.9),
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

        # REST fetch는 진행 (diagnostic) — 단 first attempt에서 short-circuit으로 1회만
        self.assertGreaterEqual(mock_fetch.await_count, 1)
        # DB/Redis write 차단
        mock_insert.assert_not_called()
        mock_redis.assert_not_called()
        # counter — rest_write_blocked +1, write 관련 counter 0
        self.assertEqual(controller.counters["rest_write_blocked"], 1)
        # short-circuit — first attempt에서 True 반환 후 success counter +1
        self.assertEqual(controller.counters["success"], 1)
        self.assertEqual(controller.counters["attempted"], 1)

    async def test_rest_write_blocked_finalizer_disabled_rollback(self):
        """finalizer=false rollback path + REST_WRITE_ENABLED=false → write 미호출 + retry short-circuit."""
        from app import config
        controller = self._make_controller()
        contract = _make_contract()
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", False), \
             patch.object(config, "KRX_CLOSE_REST_WRITE_ENABLED", False), \
             patch(
                 "app.crawlers.krx_kis.fetch_kis_futures_quote",
                 new=AsyncMock(return_value=_make_rest_result("1500.2")),
             ) as mock_fetch, patch(
                 "app.crud.get_latest_source_rate",
                 return_value=_recent_last(1500.9),
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

        # finalizer=false rollback path도 first attempt에서 short-circuit
        # (3회 retry 안 함 — 같은 stale 값 3회 확인 무의미)
        self.assertEqual(mock_fetch.await_count, 1)
        mock_insert.assert_not_called()
        mock_redis.assert_not_called()
        self.assertEqual(controller.counters["rest_write_blocked"], 1)
        self.assertEqual(controller.counters["success"], 1)
        self.assertEqual(controller.counters["attempted"], 1)

    async def test_rest_write_enabled_true_gated_write(self):
        """REST_WRITE_ENABLED=true + gate 전부 통과 → write 수행.

        2026-06-10 #4: 구 의미("무가드 write 복원")는 폐기 — true는 gate-checked
        write. 본 테스트 mock은 gate-passing(resp_* 일치 + fresh last)이라 write
        도달. gate reject 분기는 GateChain 테스트 클래스에서 검증.
        """
        from app import config
        controller = self._make_controller()
        contract = _make_contract()
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", False), \
             patch.object(config, "KRX_CLOSE_REST_WRITE_ENABLED", True), \
             patch(
                 "app.crawlers.krx_kis.fetch_kis_futures_quote",
                 new=AsyncMock(return_value=_make_rest_result("1500.2")),
             ), patch(
                 "app.crud.get_latest_source_rate",
                 return_value=_recent_last(1500.9),
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

        # flag=true → 기존 동작: DB + Redis write 호출
        mock_insert.assert_called_once()
        mock_redis.assert_called_once()
        # rest_write_blocked counter 0 (flag true라 차단 안 함)
        self.assertEqual(controller.counters["rest_write_blocked"], 0)
        self.assertEqual(controller.counters["success"], 1)


# ---------------------------------------------------------------------------
# 2026-06-10 #4 — close REST write gate chain (_evaluate_close_write_gates)
# ---------------------------------------------------------------------------

class TestKrxCloseSnapshotControllerGateChain(unittest.TestCase):
    """gate chain 판정 단위 매트릭스 (pure — DB/Redis touch 없음)."""

    def setUp(self):
        self.controller = KrxCloseSnapshotController(token_manager=MagicMock())
        self.contract = _make_contract()
        self.boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))

    def _eval(self, *, result=None, contract=None, boundary=None, last="default"):
        if last == "default":
            last = _recent_last()
        return self.controller._evaluate_close_write_gates(
            result=result or _make_rest_result(),
            contract=contract or self.contract,
            boundary_at_kst=boundary or self.boundary,
            last=last,
        )

    def test_all_gates_pass(self):
        self.assertIsNone(self._eval())

    def test_gate1_calendar_holiday_rejects(self):
        # 5/25 부처님오신날 대체공휴일 (실제 캘린더 — 5/25 사고 1차 차단 회귀 잠금)
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 25))
        self.assertEqual(self._eval(boundary=boundary), "calendar")

    def test_gate2_contract_month_mismatch_rejects(self):
        result = _make_rest_result()
        result["resp_hts_kor_isnm"] = "미국달러 F 202606"
        self.assertEqual(self._eval(result=result), "contract")

    def test_gate2_expiry_date_mismatch_rejects(self):
        result = _make_rest_result()
        result["resp_futs_last_tr_date"] = "20260615"
        self.assertEqual(self._eval(result=result), "contract")

    def test_gate2_resp_fields_missing_rejects(self):
        # fail-closed: 응답 원본 필드 부재 → contract reject
        result = _make_rest_result()
        result["resp_hts_kor_isnm"] = None
        self.assertEqual(self._eval(result=result), "contract")

    def test_gate2_expired_contract_rejects(self):
        # 만기(5/18) 경과 후 boundary(5/19 화 영업일) → contract reject.
        # gate 3 통과 가능한 fresh last를 줘서 gate 2 단독 검증.
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 19))
        last = {"rate": 1500.9, "timestamp": "2026-05-19T15:40:00+09:00"}
        self.assertEqual(self._eval(boundary=boundary, last=last), "contract")

    def test_gate3_last_none_rejects(self):
        # 구 동작(last=None → sanity skip → write 진행) 구멍 폐쇄
        self.assertEqual(self._eval(last=None), "session_evidence")

    def test_gate3_stale_last_rejects_unregistered_holiday_sim(self):
        """캘린더 미등록 휴장 시뮬 — gate 3 존재 증명 케이스.

        gate 1(라이브러리)은 영업일로 통과하지만 마지막 WS tick이 3일 전(5/12)이면
        시장이 오늘 실제로 안 열렸다는 자기 데이터 증거 → session_evidence reject.
        (5/25 사고 재현은 hotfix 이후 gate 1이 잡으므로, gate 3의 가치는 이
        미등록-휴장 케이스로 증명된다.)
        """
        last = {"rate": 1500.9, "timestamp": "2026-05-12T15:45:00+09:00"}
        self.assertEqual(self._eval(last=last), "session_evidence")

    def test_gate3_unparseable_timestamp_rejects(self):
        last = {"rate": 1500.9, "timestamp": "..."}
        self.assertEqual(self._eval(last=last), "session_evidence")

    def test_gate3_exactly_threshold_passes(self):
        # 경계값: age == 3h(default) → pass (strict >)
        last = {"rate": 1500.9, "timestamp": "2026-05-15T12:45:00+09:00"}
        self.assertIsNone(self._eval(last=last))

    def test_gate3_just_over_threshold_rejects(self):
        last = {"rate": 1500.9, "timestamp": "2026-05-15T12:44:59+09:00"}
        self.assertEqual(self._eval(last=last), "session_evidence")

    def test_gate3_future_timestamp_passes(self):
        # boundary 이후 late tick (음수 age) → pass
        last = {"rate": 1500.9, "timestamp": "2026-05-15T15:45:30+09:00"}
        self.assertIsNone(self._eval(last=last))

    def test_gate3_naive_timestamp_treated_as_kst(self):
        last = {"rate": 1500.9, "timestamp": "2026-05-15T15:35:00"}
        self.assertIsNone(self._eval(last=last))

    def test_incident_0609_scenario_passes(self):
        """6/9 사고 재현 — last tick 15:04 (boundary −41min) → 전 gate 통과.

        gate-checked write가 있었다면 6/9 종가(1514.7)는 same-day 보존됐다.
        """
        boundary = compute_close_boundary_kst("CF", date(2026, 6, 9))
        contract = ContractInfo(
            short_code="A75606", standard_code="KR4A75660006",
            name="미국달러 F 202606", contract_month="202606",
            expiry_date=date(2026, 6, 15),
        )
        result = _make_rest_result("1514.7")
        result["resp_hts_kor_isnm"] = "미국달러 F 202606"
        result["resp_futs_last_tr_date"] = "20260615"
        last = {"rate": 1510.6, "timestamp": "2026-06-09T15:04:00+09:00"}
        verdict = self.controller._evaluate_close_write_gates(
            result=result, contract=contract, boundary_at_kst=boundary, last=last,
        )
        self.assertIsNone(verdict)


class TestKrxCloseSnapshotControllerGateChainIntegration(unittest.IsolatedAsyncioTestCase):
    """gate chain end-to-end — shadow counter/short-circuit + daily append tail."""

    def _make_controller(self) -> KrxCloseSnapshotController:
        return KrxCloseSnapshotController(
            token_manager=MagicMock(),
            retry_delays_sec={"CF": [0.0, 0.0, 0.0], "CM": [0.0, 0.0, 0.0]},
            sanity_pct=0.02,
            rest_timeout_sec=1.0,
        )

    async def _run_snapshot(self, controller, *, session="CF", boundary=None,
                            last=None, flag=False, daily_enabled=False,
                            rest_result=None):
        """공통 실행 harness — patch 묶음 + schedule + drain. mock 3종 반환."""
        from app import config
        contract = _make_contract()
        if boundary is None:
            boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", False), \
             patch.object(config, "KRX_CLOSE_REST_WRITE_ENABLED", flag), \
             patch.object(config, "KRX_DAILY_APPEND_ENABLED", daily_enabled), \
             patch(
                 "app.crawlers.krx_kis.fetch_kis_futures_quote",
                 new=AsyncMock(return_value=rest_result or _make_rest_result()),
             ), patch(
                 "app.crud.get_latest_source_rate", return_value=last,
             ), patch(
                 "app.crud.insert_source_rate_if_changed", return_value=True,
             ) as mock_insert, patch(
                 "app.latest_rates_cache.set_latest_krx_rate_from_sync_job",
                 return_value=True,
             ) as mock_redis, patch(
                 "app.source_daily_rates.append_krx_cf_daily_row",
                 return_value=("INSERT", "신규 date — insert"),
             ) as mock_append, patch(
                 "app.database.get_db_context",
             ):
            controller.schedule_close_snapshot(
                contract=contract, session=session, boundary_at_kst=boundary,
            )
            await asyncio.sleep(0.1)
            await controller.close(timeout=1.0)
        return mock_insert, mock_redis, mock_append

    async def test_gate_reject_shadow_with_flag_false(self):
        """flag=false에서도 gate 평가 (shadow) — reject counter + write 차단 + short-circuit."""
        controller = self._make_controller()
        stale_last = {"rate": 1500.9, "timestamp": "2026-05-12T15:45:00+09:00"}
        mock_insert, mock_redis, mock_append = await self._run_snapshot(
            controller, last=stale_last, flag=False,
        )
        self.assertEqual(
            controller.counters["rest_write_gate_rejected_session_evidence"], 1)
        self.assertEqual(controller.counters["rest_write_blocked"], 0)  # gate가 먼저
        mock_insert.assert_not_called()
        mock_redis.assert_not_called()
        mock_append.assert_not_called()
        self.assertEqual(controller.counters["success"], 1)   # short-circuit
        self.assertEqual(controller.counters["attempted"], 1)  # retry 안 함

    async def test_gate_reject_with_flag_true_blocks_write(self):
        """flag=true여도 gate reject면 write 안 함 (5/25 모드 — calendar)."""
        controller = self._make_controller()
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 25))  # 대체공휴일
        mock_insert, mock_redis, mock_append = await self._run_snapshot(
            controller, boundary=boundary, last=_recent_last(), flag=True,
        )
        self.assertEqual(controller.counters["rest_write_gate_rejected_calendar"], 1)
        mock_insert.assert_not_called()
        mock_redis.assert_not_called()
        mock_append.assert_not_called()

    async def test_gates_pass_flag_false_blocked_shadow_evidence(self):
        """gates 통과 + flag=false → rest_write_blocked (gates=passed shadow 증거)."""
        controller = self._make_controller()
        mock_insert, mock_redis, mock_append = await self._run_snapshot(
            controller, last=_recent_last(), flag=False,
        )
        self.assertEqual(controller.counters["rest_write_blocked"], 1)
        for reason in ("calendar", "contract", "session_evidence"):
            self.assertEqual(
                controller.counters[f"rest_write_gate_rejected_{reason}"], 0)
        mock_insert.assert_not_called()
        mock_redis.assert_not_called()
        mock_append.assert_not_called()

    async def test_gates_pass_flag_true_writes_and_appends_daily(self):
        """gates 통과 + flag=true + daily enabled + CF → write + daily append tail."""
        controller = self._make_controller()
        mock_insert, mock_redis, mock_append = await self._run_snapshot(
            controller, last=_recent_last(), flag=True, daily_enabled=True,
        )
        mock_insert.assert_called_once()
        mock_redis.assert_called_once()
        mock_append.assert_called_once()
        # (db, date_kst, close, contract_code) + metadata_extra origin 마커
        args, kwargs = mock_append.call_args
        self.assertEqual(args[1], date(2026, 5, 15))
        self.assertEqual(args[2], 1500.2)
        self.assertEqual(args[3], "A75605")
        self.assertEqual(kwargs["metadata_extra"], {"origin": "rest_close_write"})
        self.assertEqual(controller.counters["success"], 1)

    async def test_cm_session_no_daily_append(self):
        """CM 세션은 daily append 대상 아님 (write는 수행)."""
        controller = self._make_controller()
        # CM 06:00 금요일(5/15) — 영업일. last는 06:00 기준 fresh.
        boundary = compute_close_boundary_kst("CM", date(2026, 5, 15))
        last = {"rate": 1500.9, "timestamp": "2026-05-15T05:30:00+09:00"}
        mock_insert, mock_redis, mock_append = await self._run_snapshot(
            controller, session="CM", boundary=boundary, last=last,
            flag=True, daily_enabled=True,
        )
        mock_insert.assert_called_once()
        mock_redis.assert_called_once()
        mock_append.assert_not_called()

    async def test_daily_append_disabled_not_called(self):
        controller = self._make_controller()
        mock_insert, _, mock_append = await self._run_snapshot(
            controller, last=_recent_last(), flag=True, daily_enabled=False,
        )
        mock_insert.assert_called_once()
        mock_append.assert_not_called()

    async def test_daily_append_exception_isolated(self):
        """append 예외 → 격리 (close write success 유지)."""
        from app import config
        controller = self._make_controller()
        contract = _make_contract()
        boundary = compute_close_boundary_kst("CF", date(2026, 5, 15))
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", False), \
             patch.object(config, "KRX_CLOSE_REST_WRITE_ENABLED", True), \
             patch.object(config, "KRX_DAILY_APPEND_ENABLED", True), \
             patch(
                 "app.crawlers.krx_kis.fetch_kis_futures_quote",
                 new=AsyncMock(return_value=_make_rest_result()),
             ), patch(
                 "app.crud.get_latest_source_rate", return_value=_recent_last(),
             ), patch(
                 "app.crud.insert_source_rate_if_changed", return_value=True,
             ) as mock_insert, patch(
                 "app.latest_rates_cache.set_latest_krx_rate_from_sync_job",
                 return_value=True,
             ), patch(
                 "app.source_daily_rates.append_krx_cf_daily_row",
                 side_effect=RuntimeError("append boom"),
             ) as mock_append, patch(
                 "app.database.get_db_context",
             ):
            controller.schedule_close_snapshot(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )
            await asyncio.sleep(0.1)
            await controller.close(timeout=1.0)

        mock_insert.assert_called_once()
        mock_append.assert_called_once()
        self.assertEqual(controller.counters["success"], 1)  # write 성공 유지


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
