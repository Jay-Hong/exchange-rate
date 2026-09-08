"""IBK 1단계: 기존 제어 흐름과 provisional 결과를 함께 잠근다.

MIBANK 저장/거짓 성공 등 기존 결함을 고친 테스트가 아니다. 후속 정책 활성 전
HTTP 순서·쓰기·재시도·runner exit 의미를 바꾸지 않는다는 characterization이다.
모든 외부 I/O와 sleep은 mock이며 운영 DB/Chrome/Telegram을 사용하지 않는다.
"""

import datetime
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from app.crawlers import ibk, runner


@pytest.fixture
def flow(monkeypatch):
    now = ibk.KST.localize(datetime.datetime(2026, 9, 7, 12, 0))
    clock = SimpleNamespace(datetime=SimpleNamespace(now=lambda tz: now))
    monkeypatch.setattr(ibk, "datetime", clock)
    mocks = SimpleNamespace(
        clock=clock,
        db=MagicMock(),
        current=MagicMock(return_value=False),
        dated=MagicMock(return_value=ibk.DatedRequestOutcome.FALLBACK),
        selenium=MagicMock(return_value=0),
        reliable=MagicMock(return_value=True),
        mibank=MagicMock(return_value=(
            {"usd-krw": 1350.0, "jpy-krw": 860.0, "eur-krw": 1550.0},
            {"hard_fail": False, "soft_fail": False, "details": {}},
        )),
        insert=MagicMock(return_value=3),
        sleep=MagicMock(),
        original_selenium=ibk.crawl_and_save_ibk_routine_selenium,
        original_current=ibk.try_crawl_with_requests,
    )
    monkeypatch.setattr(ibk, "SessionLocal", MagicMock(return_value=mocks.db))
    for name, mock in (
        ("try_crawl_with_requests", mocks.current),
        ("try_crawl_with_dated_requests", mocks.dated),
        ("crawl_and_save_ibk_routine_selenium", mocks.selenium),
        ("is_mibank_rate_reliable", mocks.reliable),
        ("_crawl_mibank_ibk", mocks.mibank),
    ):
        monkeypatch.setattr(ibk, name, mock)
    monkeypatch.setattr(ibk.crud, "insert_bank_rates_into_db", mocks.insert)
    monkeypatch.setattr(ibk.time, "sleep", mocks.sleep)
    # Any accidentally unmocked network/driver access fails the test immediately.
    def forbidden(*args, **kwargs):
        raise AssertionError("external I/O is forbidden in IBK characterization")
    monkeypatch.setattr(ibk.requests, "get", forbidden)
    monkeypatch.setattr(ibk.requests, "post", forbidden)
    monkeypatch.setattr(ibk, "selenium_driver_context", forbidden)
    return mocks


@pytest.mark.parametrize("hour,minute,second,get_first", [
    (7, 59, 59, False), (8, 0, 0, True),
    (8, 34, 59, True), (8, 35, 0, True),
])
def test_http_order_remains_legacy(flow, hour, minute, second, get_first):
    now = ibk.KST.localize(datetime.datetime(2026, 9, 7, hour, minute, second))
    flow.clock.datetime.now = lambda tz: now
    flow.current.return_value = True
    flow.dated.return_value = ibk.DatedRequestOutcome.OBSERVED
    result = ibk.crawl_ibk_legacy_result()
    if get_first:
        assert result.disposition is ibk.IbkLegacyDisposition.CURRENT_REQUEST_RETURNED
        flow.current.assert_called_once_with(flow.db, reference_time=now)
        flow.dated.assert_not_called()
    else:
        assert result.disposition is ibk.IbkLegacyDisposition.DATED_OBSERVED
        flow.current.assert_not_called()
        flow.dated.assert_called_once_with(flow.db, reference_time=now)
    flow.selenium.assert_not_called()
    flow.mibank.assert_not_called()
    flow.db.close.assert_called_once_with()


@pytest.mark.parametrize("outcome,disposition", [
    (ibk.DatedRequestOutcome.OBSERVED, ibk.IbkLegacyDisposition.DATED_OBSERVED),
    (ibk.DatedRequestOutcome.PRESERVED, ibk.IbkLegacyDisposition.DATED_PRESERVED),
])
def test_current_failure_then_dated_terminal_stops_fallback(flow, outcome, disposition):
    order = MagicMock()
    order.attach_mock(flow.current, "get")
    order.attach_mock(flow.dated, "post")
    flow.dated.return_value = outcome
    result = ibk.crawl_ibk_legacy_result()
    assert result == ibk.IbkLegacyResult(disposition)
    assert [c[0] for c in order.mock_calls] == ["get", "post"]
    flow.selenium.assert_not_called()
    flow.mibank.assert_not_called()
    flow.insert.assert_not_called()


@pytest.mark.parametrize("minute,second,suppressed", [(0, 0, True), (4, 59, True), (5, 0, False)])
def test_midnight_selenium_boundary_preserved(flow, minute, second, suppressed):
    now = ibk.KST.localize(datetime.datetime(2026, 9, 8, 0, minute, second))
    flow.clock.datetime.now = lambda tz: now
    result = ibk.crawl_ibk_legacy_result()
    flow.current.assert_not_called()
    flow.dated.assert_called_once_with(flow.db, reference_time=now)
    expected = (ibk.IbkLegacyDisposition.MIDNIGHT_SUPPRESSED if suppressed
                else ibk.IbkLegacyDisposition.SELENIUM_RETURNED)
    assert result.disposition is expected
    assert flow.selenium.call_count == int(not suppressed)
    flow.mibank.assert_not_called()


@pytest.mark.parametrize("attempts", [1, 2, 3])
def test_selenium_return_stops_retries_even_for_zero(flow, attempts):
    flow.selenium.side_effect = [RuntimeError("test failure")] * (attempts - 1) + [0]
    result = ibk.crawl_ibk_legacy_result()
    assert result == ibk.IbkLegacyResult(
        ibk.IbkLegacyDisposition.SELENIUM_RETURNED, attempts, 0,
    )
    assert flow.selenium.call_count == attempts
    assert flow.sleep.call_args_list == [call(2)] * (attempts - 1)
    flow.mibank.assert_not_called()
    flow.db.close.assert_called_once_with()


def test_selenium_call_carries_the_run_anchor(flow):
    """운영 호출자가 회차 시작 시각을 넘겨야 shadow 예산 가드가 동작한다.

    ⛔ 이 배선이 빠지면 가드는 예외도 실패도 없이 **조용히** 꺼진다 — 관측이 예산을 초과해도
       건너뛰지 않는다. 기준을 모듈 import 시각으로 재던 판이 전체 스위트에서 드러났듯,
       조용한 무력화는 격리 시험으로는 안 보인다.
    """
    import time as _time

    before = _time.monotonic()
    ibk.crawl_ibk_legacy_result()
    after = _time.monotonic()

    anchor_value = flow.selenium.call_args.kwargs.get("run_started_at")
    assert anchor_value is not None, "회차 시작 시각이 전달되지 않으면 가드가 꺼진다"
    assert before <= anchor_value <= after, "이 회차의 시각이어야 한다"


@pytest.mark.parametrize("soft_fail,write_return", [(False, 0), (False, 3), (True, 3)])
def test_all_selenium_failures_still_reach_legacy_mibank_writer(flow, soft_fail, write_return):
    flow.selenium.side_effect = RuntimeError("test failure")
    rates, evaluation = flow.mibank.return_value
    evaluation["soft_fail"] = soft_fail
    flow.insert.return_value = write_return
    result = ibk.crawl_ibk_legacy_result()
    assert result == ibk.IbkLegacyResult(
        ibk.IbkLegacyDisposition.MIBANK_WRITE_RETURNED, 3, write_return,
    )
    assert flow.selenium.call_count == 3
    assert flow.sleep.call_args_list == [call(2), call(2)]
    flow.mibank.assert_called_once_with(flow.db)
    flow.insert.assert_called_once_with(db=flow.db, current_rates=rates, bank_name="ibk")
    flow.db.close.assert_called_once_with()


@pytest.mark.parametrize("case,disposition", [
    ("hard_fail", ibk.IbkLegacyDisposition.MIBANK_REJECTED),
    ("unreliable", ibk.IbkLegacyDisposition.MIBANK_SKIPPED),
    ("fetch_failed", ibk.IbkLegacyDisposition.MIBANK_FAILED),
    ("db_failed", ibk.IbkLegacyDisposition.MIBANK_FAILED),
])
def test_mibank_nonwrite_and_failure_endings_still_do_not_raise(flow, case, disposition):
    flow.selenium.side_effect = RuntimeError("test failure")
    if case == "hard_fail":
        flow.mibank.return_value[1]["hard_fail"] = True
    elif case == "unreliable":
        flow.reliable.return_value = False
    elif case == "fetch_failed":
        flow.mibank.side_effect = RuntimeError("test failure")
    else:
        flow.insert.side_effect = RuntimeError("test DB failure")
    assert ibk.crawl_ibk_legacy_result() == ibk.IbkLegacyResult(disposition, 3)
    if case != "db_failed":
        flow.insert.assert_not_called()
    if case == "unreliable":
        flow.mibank.assert_not_called()
    flow.db.close.assert_called_once_with()


def test_dated_exception_can_reach_mibank_without_selenium(flow):
    flow.dated.side_effect = RuntimeError("test DB failure")
    result = ibk.crawl_ibk_legacy_result()
    assert result == ibk.IbkLegacyResult(ibk.IbkLegacyDisposition.MIBANK_WRITE_RETURNED, 0, 3)
    flow.selenium.assert_not_called()
    flow.sleep.assert_not_called()
    flow.mibank.assert_called_once_with(flow.db)


def test_real_selenium_helper_rethrows_and_outer_loop_retries(flow, monkeypatch, caplog):
    driver = MagicMock()
    driver.__enter__.return_value.get.side_effect = RuntimeError("test driver failure")
    monkeypatch.setattr(ibk, "selenium_driver_context", MagicMock(return_value=driver))
    monkeypatch.setattr(ibk, "crawl_and_save_ibk_routine_selenium", flow.original_selenium)
    result = ibk.crawl_ibk_legacy_result()
    assert result.selenium_attempts == 3
    assert result.disposition is ibk.IbkLegacyDisposition.MIBANK_WRITE_RETURNED
    assert driver.__exit__.call_count == 3
    retry_records = [r for r in caplog.records if "Selenium 실패 (시도" in r.getMessage()]
    assert len(retry_records) == 3
    # A bank-field-only log filter misses these outer warnings.
    assert all(not hasattr(r, "bank") for r in retry_records)


def test_current_crud_zero_is_only_a_legacy_return_not_verified_observation(flow, monkeypatch):
    response = MagicMock()
    monkeypatch.setattr(ibk.requests, "get", MagicMock(return_value=response))
    monkeypatch.setattr(ibk, "_parse_ibk_official_response", MagicMock(return_value=(
        flow.mibank.return_value[0], "12:00:00",
    )))
    monkeypatch.setattr(ibk, "try_crawl_with_requests", flow.original_current)
    flow.insert.return_value = 0
    result = ibk.crawl_ibk_legacy_result()
    assert result == ibk.IbkLegacyResult(ibk.IbkLegacyDisposition.CURRENT_REQUEST_RETURNED)
    assert not hasattr(result, "status")  # Final semantic status is not enabled.
    flow.insert.assert_called_once()
    flow.dated.assert_not_called()


@pytest.mark.parametrize("fail_at", ["session", "close"])
def test_session_lifecycle_exceptions_still_propagate(flow, monkeypatch, fail_at):
    failure = RuntimeError("test DB lifecycle failure")
    flow.current.return_value = True
    if fail_at == "session":
        monkeypatch.setattr(ibk, "SessionLocal", MagicMock(side_effect=failure))
    else:
        flow.db.close.side_effect = failure
    with pytest.raises(RuntimeError) as raised:
        ibk.crawl_and_save_ibk_bank_exchange_rates()
    assert raised.value is failure
    if fail_at == "session":
        flow.db.close.assert_not_called()
    flow.mibank.assert_not_called()


@pytest.mark.parametrize("disposition", list(ibk.IbkLegacyDisposition))
def test_public_entry_discards_provisional_result_for_all_paths(monkeypatch, disposition):
    result = ibk.IbkLegacyResult(disposition)
    run = MagicMock(return_value=result)
    monkeypatch.setattr(ibk, "crawl_ibk_legacy_result", run)
    assert ibk.crawl_and_save_ibk_bank_exchange_rates() is None
    run.assert_called_once_with()
    with pytest.raises(FrozenInstanceError):
        result.selenium_attempts = 1


@pytest.mark.parametrize("case,exit_code", [("get", 0), ("all_failed", 0), ("db_close", 1)])
def test_real_ibk_entry_through_runner_keeps_legacy_exit_semantics(flow, monkeypatch, case, exit_code):
    from app import atomic_write_refresh
    monkeypatch.setattr(atomic_write_refresh, "refresh_write_mode_cache", MagicMock())
    monkeypatch.setattr(runner.sys, "argv", ["runner", "ibk"])
    monkeypatch.setitem(runner.CRAWLER_MAP, "ibk", ibk.crawl_and_save_ibk_bank_exchange_rates)
    if case == "all_failed":
        flow.selenium.side_effect = RuntimeError("test driver failure")
        flow.mibank.side_effect = RuntimeError("test request failure")
    else:
        flow.current.return_value = True
    if case == "db_close":
        flow.db.close.side_effect = RuntimeError("test DB close failure")
    with pytest.raises(SystemExit) as raised:
        runner.main()
    assert raised.value.code == exit_code
    flow.db.close.assert_called_once_with()
