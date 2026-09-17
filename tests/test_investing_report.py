"""슬라이스 1: 실제 파싱/오케스트레이션 + HTTP/DB 대역, 보고와 실행의 분리."""

import asyncio
import json
import logging
import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app import scheduler
from app.crawlers import constants, investing, investing_report
from app.logging import CustomJsonFormatter


PAIRS = tuple(investing.INVESTING_SELECTORS)
NORMAL = {"usd-krw": "1,350", "jpy-krw": "9", "eur-krw": "1,500"}


def response(values=None, *, status=200, dxy=True):
    values = NORMAL if values is None else values
    html = "".join(
        f'<span id="{investing.INVESTING_SELECTORS[pair][1:]}">{text}</span>'
        for pair, text in values.items()
    )
    if dxy:
        html += '<span id="sb_last_8827">100</span>'
    return SimpleNamespace(status_code=status, text=html, raise_for_status=Mock())


@pytest.fixture
def harness(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=investing.logger.name)
    # 어느 코드가 대역 경계를 우회해도 실제 HTTP/DB 세션을 열 수 없다.
    def forbidden(*args, **kwargs):
        raise AssertionError("real I/O forbidden")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(investing.cffi_requests, "get", forbidden)
    monkeypatch.setattr(investing, "_consecutive_403", 0)
    monkeypatch.setattr(investing, "_cooldown_until", 0.0)
    monkeypatch.setattr(investing, "_blocked", False)
    monkeypatch.setattr(investing, "_last_block_summary", 0.0)
    monkeypatch.setattr(investing.crud, "_write_mode_skip_counts", {})
    monkeypatch.setattr(investing.time, "sleep", Mock())
    db = Mock()
    session = Mock(return_value=db)
    http = Mock(return_value=response())
    writer = Mock(return_value=3)
    actual_writer = investing.crud.insert_investing_rates_into_db
    dxy = Mock(return_value=1)
    fallback = Mock()
    monkeypatch.setattr(investing, "SessionLocal", session)
    monkeypatch.setattr(investing, "_http_get", http)
    monkeypatch.setattr(investing.crud, "insert_investing_rates_into_db", writer)
    monkeypatch.setattr(investing.crud, "insert_market_index_rate_into_db", dxy)
    monkeypatch.setattr(investing, "_try_dxy_futures_fallback", fallback)

    def events(name=None):
        parsed = [json.loads(record.getMessage()) for record in caplog.records
                  if record.name == investing.logger.name and record.getMessage().startswith('{')]
        return [event for event in parsed if name is None or event["event"] == name]

    return SimpleNamespace(db=db, session=session, http=http, writer=writer, dxy=dxy,
                           fallback=fallback, events=events, actual_writer=actual_writer)


def finished(h):
    reports = h.events("investing_round_finished")
    assert len(reports) == 1
    return reports[0]


def collection(report):
    """v2 전송 형식에서 통화별 증거를 읽는다 (상태 우선순위를 재계산하지 않음)."""
    if report["format"] == "compact":
        return {pair: {"status": "valid", "reason": "validated", "normalized_rate": rate,
                       "attempt_id": report["fx_attempt_id"]}
                for pair, rate in report["rates"].items()}
    attempts = {a["attempt_id"]: a for a in report["attempts"]}
    result = {}
    for pair, attempt_id in report["collection_attempts"].items():
        attempt = attempts[attempt_id]
        observed = (attempt["collection"][pair] if "collection" in attempt else
                    {"status": "not_attempted", "reason": "not_started"})
        result[pair] = {**observed, "attempt_id": attempt_id}
    return result


def assert_unknown_storage(report, submitted=PAIRS):
    assert report["final_db"] == "not_checked"
    if report["format"] == "compact":
        assert set(submitted) == set(report["rates"])
        assert report["writing"] == "per_currency_write_unverified"
    else:
        for pair in PAIRS:
            assert report["writing"][pair]["status"] == (
                "unknown" if pair in submitted else "not_attempted"
            )


def test_normal_order_and_round_identity(harness):
    h = harness
    order = []

    def session():
        assert len(h.events("investing_round_started")) == 1
        order.append("session")
        return h.db

    def writer(**kwargs):
        assert kwargs == {"db": h.db, "current_rates": {
            "usd-krw": 1350.0, "jpy-krw": 900.0, "eur-krw": 1500.0}}
        order.append("fx")
        return 3

    def dxy(**kwargs):
        evidence, = h.events("investing_fx_evidence")
        assert evidence["attempt_id"] == 1
        assert evidence["writer_returned_count"] == 3
        order.append("dxy")

    def close():
        assert not h.events("investing_round_finished")
        order.append("close")

    h.session.side_effect, h.writer.side_effect = session, writer
    h.dxy.side_effect, h.db.close.side_effect = dxy, close
    assert investing.crawl_and_save_investing_exchange_rates() is None
    report = finished(h)
    assert order == ["session", "fx", "dxy", "close"]
    assert len({event["round_id"] for event in h.events()}) == 1
    assert len(report["round_id"]) == 32
    assert report["session"] == "closed"
    assert report["execution"] == {
        "status": "normal", "reason": "returned", "exception_propagated": False}
    assert all(item["status"] == "valid" for item in collection(report).values())
    assert report["format"] == "compact"
    assert report["fx_attempt_id"] == 1
    assert "attempts" not in report
    assert report["rates"] == {"usd-krw": 1350.0, "jpy-krw": 900.0, "eur-krw": 1500.0}
    started, evidence, _ = h.events()
    assert started["format"] == "lifecycle"
    assert "collection" not in started and "attempts" not in started
    assert evidence["format"] == "compact"
    assert evidence["rates"] == report["rates"]
    assert evidence["execution"]["status"] == "running"
    assert all(event["schema_version"] == 2 for event in h.events())
    h.http.assert_called_once()
    assert_unknown_storage(report)


@pytest.mark.parametrize("zero_reason", ["unchanged", "uninitialized", "halt"])
def test_zero_return_does_not_infer_unchanged_or_policy_block(harness, monkeypatch, zero_reason):
    h = harness
    # 실제 writer의 서로 다른 0 반환 분기를 대역 DB로 거친다. 보고자는 사유를 모른다.
    h.writer.side_effect = h.actual_writer
    if zero_reason == "unchanged":
        h.db.query.return_value.filter.return_value.order_by.return_value.first.side_effect = [
            SimpleNamespace(rate=rate) for rate in (1350.0, 900.0, 1500.0)]
    elif zero_reason == "uninitialized":
        monkeypatch.setattr(investing.crud.atomic_write_runtime, "is_initialized", lambda: False)
    else:
        monkeypatch.setattr(investing.crud.atomic_write_runtime, "snapshot",
                            lambda: SimpleNamespace(enforced_action="halt"))
    investing.crawl_and_save_investing_exchange_rates()
    report = finished(h)
    assert report["writer_returned_count"] == 0
    assert all(item["status"] == "valid" for item in collection(report).values())
    assert_unknown_storage(report)
    h.http.assert_called_once()
    h.dxy.assert_called_once()
    h.db.add.assert_not_called()
    h.db.commit.assert_not_called()
    assert h.db.query.call_count == (3 if zero_reason == "unchanged" else 0)


@pytest.mark.parametrize(("text", "reason"), [
    (None, "selector_missing"), ("", "empty_or_placeholder"),
    ("   ", "empty_or_placeholder"), ("-", "empty_or_placeholder"),
    ("N/A", "empty_or_placeholder"), ("broken", "parse_failed"),
    ("nan", "nan_value"), ("inf", "out_of_range"),
    ("-inf", "out_of_range"), ("99999", "out_of_range"),
])
def test_per_currency_validation_keeps_other_currencies_and_writer_input(harness, text, reason):
    h = harness
    values = {**NORMAL, "usd-krw": text}
    if text is None:
        del values["usd-krw"]
    h.http.return_value = response(values)
    investing.crawl_and_save_investing_exchange_rates()
    report = finished(h)
    assert collection(report)["usd-krw"]["status"] == "missing"
    assert collection(report)["usd-krw"]["reason"] == reason
    assert collection(report)["jpy-krw"]["status"] == "valid"
    assert collection(report)["eur-krw"]["status"] == "valid"
    rates = h.writer.call_args.kwargs["current_rates"]
    assert rates["jpy-krw"] == 900
    assert rates["eur-krw"] == 1500
    assert ("usd-krw" in rates) == (reason in ("nan_value", "out_of_range"))
    h.http.assert_called_once()


@pytest.mark.parametrize("pair", PAIRS)
@pytest.mark.parametrize("edge", [0, 1])
def test_range_boundaries_after_normalization(harness, pair, edge):
    normalized = constants.MIBANK_RATE_RANGES[pair][edge]
    raw = normalized / investing.SCALED_CURRENCY_PAIRS.get(pair, 1)
    harness.http.return_value = response({**NORMAL, pair: str(raw)})
    investing.crawl_and_save_investing_exchange_rates()
    observed = collection(finished(harness))[pair]
    assert observed["status"] == "valid"
    assert observed["normalized_rate"] == normalized


def test_all_invalid_still_calls_writer_with_nan_infinity_and_outlier(harness):
    h = harness
    h.http.return_value = response(dict(zip(PAIRS, ("nan", "inf", "99999"))))
    investing.crawl_and_save_investing_exchange_rates()
    report = finished(h)
    assert [collection(report)[pair]["reason"] for pair in PAIRS] == [
        "nan_value", "out_of_range", "out_of_range"]
    h.writer.assert_called_once()
    rates = h.writer.call_args.kwargs["current_rates"]
    assert math.isnan(rates["usd-krw"])
    assert rates["jpy-krw"] == math.inf
    assert rates["eur-krw"] == 99999
    assert_unknown_storage(report)
    # 비표준 JSON NaN/Infinity를 생성하지 않는다.
    assert collection(report)["usd-krw"]["normalized_rate"] == "nan"


def test_all_parse_failures_never_call_writer_and_keep_legacy_normal_return(harness):
    h = harness
    h.http.return_value = response(dict(zip(PAIRS, ("-", "bad", "N/A"))))
    assert investing.crawl_and_save_investing_exchange_rates() is None
    report = finished(h)
    assert h.http.call_count == 2
    h.writer.assert_not_called()
    h.dxy.assert_not_called()
    assert_unknown_storage(report, submitted=())
    assert report["execution"]["status"] == "normal"
    assert report["execution"]["reason"] == "attempts_exhausted"
    assert all(a["status"] == "failed" for a in report["attempts"])
    assert len(h.events("investing_fx_evidence")) == 2


def test_double_403_preserves_breaker_and_no_writer(harness):
    h = harness
    h.http.return_value = response(status=403)
    assert investing.crawl_and_save_investing_exchange_rates() is None
    report = finished(h)
    assert h.http.call_count == 2
    h.writer.assert_not_called()
    assert investing._consecutive_403 == 1
    assert investing._blocked is True
    assert [a["reason"] for a in report["attempts"]] == ["http_403", "http_403"]
    assert all(item["reason"] == "http_403" for item in collection(report).values())
    assert report["execution"]["status"] == "normal"


def test_cooldown_still_creates_and_closes_session_without_http(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(investing, "_cooldown_until", investing.time.monotonic() + 600)
    monkeypatch.setattr(investing, "_consecutive_403", 5)
    assert investing.crawl_and_save_investing_exchange_rates() is None
    report = finished(h)
    h.session.assert_called_once()
    h.db.close.assert_called_once()
    h.http.assert_not_called()
    h.writer.assert_not_called()
    assert investing._consecutive_403 == 5
    assert all(a["status"] == "policy_skipped" for a in report["attempts"])
    assert all(item["status"] == "not_attempted" for item in collection(report).values())
    assert report["execution"]["reason"] == "cooldown"


@pytest.mark.parametrize("second_timeout", [False, True])
def test_dxy_error_preserves_first_fx_evidence_through_retry(harness, second_timeout):
    h = harness
    h.http.side_effect = [response(), TimeoutError("second") if second_timeout else response({})]
    original = RuntimeError("DXY failed after FX")

    def dxy(**kwargs):
        assert len(h.events("investing_fx_evidence")) == 1
        raise original

    h.dxy.side_effect = dxy
    assert investing.crawl_and_save_investing_exchange_rates() is None
    report = finished(h)
    assert [call.args[0] for call in h.http.call_args_list] == [
        investing.FIRST_INVESTING_URL, investing.SECOND_INVESTING_URL]
    first = report["attempts"][0]
    assert first["writer"]["returned_count"] == 3
    assert first["execution"]["error_type"] == "RuntimeError"
    assert all(item["status"] == "valid" and item["attempt_id"] == 1
               for item in collection(report).values())
    assert report["execution"]["status"] == ("timeout" if second_timeout else "normal")
    assert report["execution"]["exception_propagated"] is False
    h.writer.assert_called_once()
    assert_unknown_storage(report)


def test_evidence_precedes_dxy_fallback(harness):
    h = harness
    h.http.return_value = response(dxy=False)
    h.fallback.side_effect = lambda db: h.events("investing_fx_evidence")[0]
    investing.crawl_and_save_investing_exchange_rates()
    h.fallback.assert_called_once_with(h.db)
    h.dxy.assert_not_called()
    assert finished(h)["execution"]["status"] == "normal"


@pytest.mark.parametrize("phase", ["create", "close"])
def test_session_exceptions_are_reported_then_propagated_unchanged(harness, phase):
    h = harness
    error = RuntimeError(phase)
    (h.session if phase == "create" else h.db.close).side_effect = error
    with pytest.raises(RuntimeError) as caught:
        investing.crawl_and_save_investing_exchange_rates()
    assert caught.value is error
    report = finished(h)
    assert report["execution"] == {
        "status": "abnormal", "reason": "exception", "error_type": "RuntimeError",
        "exception_propagated": True}
    assert report["session"] == ("creating" if phase == "create" else "closing")
    if phase == "create":
        h.db.close.assert_not_called()
        h.http.assert_not_called()
    else:
        h.db.close.assert_called_once()
        assert all(item["status"] == "valid" for item in collection(report).values())


def test_writer_exception_is_not_commit_evidence(harness):
    h = harness
    h.writer.side_effect = RuntimeError("could fail before or after commit")
    assert investing.crawl_and_save_investing_exchange_rates() is None
    report = finished(h)
    assert h.http.call_count == h.writer.call_count == 2
    h.dxy.assert_not_called()
    for attempt in report["attempts"]:
        assert attempt["writer"]["called"] is True
        assert attempt["writer"]["returned_count"] is None
        assert attempt["writer"]["error_type"] == "RuntimeError"
    assert_unknown_storage(report)


@pytest.mark.parametrize("error", [TimeoutError(), requests.exceptions.Timeout(),
                                  investing_report.CurlTimeout("timeout")])
def test_transport_timeout_classification(harness, error):
    harness.http.side_effect = error
    investing.crawl_and_save_investing_exchange_rates()
    assert finished(harness)["execution"]["status"] == "timeout"


def test_cancellation_keeps_fx_evidence_and_propagates_without_retry(harness):
    h = harness
    error = asyncio.CancelledError()
    h.dxy.side_effect = error
    with pytest.raises(asyncio.CancelledError) as caught:
        investing.crawl_and_save_investing_exchange_rates()
    assert caught.value is error
    h.http.assert_called_once()
    h.db.close.assert_called_once()
    report = finished(h)
    assert report["execution"]["status"] == "cancelled"
    assert all(item["status"] == "valid" for item in collection(report).values())


@pytest.mark.parametrize("method", ["__init__", "session_state", "start_attempt", "observation",
                                    "writer_started", "writer_finished", "finish_attempt",
                                    "emit", "finish"])
@pytest.mark.parametrize("failure", [None, "create", "close", "dxy"])
def test_reporting_failures_never_add_retry_or_replace_original_error(harness, monkeypatch, method, failure):
    h = harness
    monkeypatch.setattr(investing_report.InvestingReport, method,
                        Mock(side_effect=ValueError("telemetry failure")))
    original = RuntimeError("original")
    if failure == "create":
        h.session.side_effect = original
    elif failure == "close":
        h.db.close.side_effect = original
    elif failure == "dxy":
        h.dxy.side_effect = [original, 1]
    if failure in ("create", "close"):
        with pytest.raises(RuntimeError) as caught:
            investing.crawl_and_save_investing_exchange_rates()
        assert caught.value is original
    else:
        assert investing.crawl_and_save_investing_exchange_rates() is None
    assert h.http.call_count == (0 if failure == "create" else 2 if failure == "dxy" else 1)
    assert h.writer.call_count == h.http.call_count
    assert h.db.close.call_count == (0 if failure == "create" else 1)


@pytest.mark.parametrize("failure_site", ["json", "logger"])
def test_serialization_and_log_sink_failures_are_isolated(harness, monkeypatch, failure_site):
    if failure_site == "json":
        monkeypatch.setattr(investing_report.json, "dumps", Mock(side_effect=TypeError("json")))
    else:
        original_info = investing.logger.info

        def fail_report_only(message, *args, **kwargs):
            if message.startswith('{'):
                raise OSError("log sink")
            return original_info(message, *args, **kwargs)

        monkeypatch.setattr(investing.logger, "info", fail_report_only)
    error = RuntimeError("close")
    harness.db.close.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        investing.crawl_and_save_investing_exchange_rates()
    assert caught.value is error
    harness.http.assert_called_once()
    harness.writer.assert_called_once()


def test_lost_writer_call_telemetry_does_not_claim_not_attempted(harness, monkeypatch):
    monkeypatch.setattr(investing_report.InvestingReport, "writer_started",
                        Mock(side_effect=RuntimeError("telemetry")))
    investing.crawl_and_save_investing_exchange_rates()
    report = finished(harness)
    harness.writer.assert_called_once()
    assert report["telemetry_errors"] == ["writer_started"]
    assert report["attempts"][0]["writer"]["called"] is None
    assert all(item["status"] == "unknown" for item in report["writing"].values())


def test_round_ids_are_distinct(harness):
    investing.crawl_and_save_investing_exchange_rates()
    investing.crawl_and_save_investing_exchange_rates()
    assert len({event["round_id"] for event in harness.events("investing_round_finished")}) == 2


def test_retry_recovery_keeps_detailed_original_attempts(harness):
    harness.http.side_effect = [TimeoutError(), response()]
    investing.crawl_and_save_investing_exchange_rates()
    report = finished(harness)
    assert report["format"] == "detail"
    assert "collection" not in report
    assert report["collection_attempts"] == dict.fromkeys(PAIRS, 2)
    assert report["attempts"][0]["execution"]["status"] == "timeout"
    assert report["attempts"][1]["collection"]["jpy-krw"]["normalized_rate"] == 900
    assert harness.events("investing_fx_evidence")[0]["format"] == "detail"


def test_lost_attempt_start_does_not_drop_fx_evidence_as_unreached(harness, monkeypatch):
    monkeypatch.setattr(investing_report.InvestingReport, "start_attempt",
                        Mock(side_effect=RuntimeError("telemetry")))
    investing.crawl_and_save_investing_exchange_rates()
    evidence, = harness.events("investing_fx_evidence")
    assert evidence["format"] == "detail"
    assert evidence["telemetry_errors"] == ["start_attempt"]
    first = evidence["attempts"][0]
    assert first["status"] == "unknown"
    assert first["reason"] == "telemetry_error"
    assert first["writer"]["called"] is True
    assert first["collection"]["usd-krw"]["normalized_rate"] == 1350


@pytest.mark.parametrize("after_start", [False, True])
@pytest.mark.parametrize("http_result", ["timeout", "403"])
def test_lost_attempt_start_with_two_http_failures(harness, monkeypatch, after_start, http_result):
    original_start = investing_report.InvestingReport.start_attempt

    def fail_start(report, attempt_id):
        if after_start:
            original_start(report, attempt_id)
        raise RuntimeError("start telemetry")

    monkeypatch.setattr(investing_report.InvestingReport, "start_attempt", fail_start)
    if http_result == "timeout":
        harness.http.side_effect = TimeoutError()
    else:
        harness.http.return_value = response(status=403)
    assert investing.crawl_and_save_investing_exchange_rates() is None
    assert harness.http.call_count == 2
    harness.writer.assert_not_called()
    report = finished(harness)
    assert report["format"] == "detail"
    assert "outcome" not in report and "rates" not in report
    assert report["telemetry_errors"] == ["start_attempt", "start_attempt"]
    expected = ({"status": "unknown", "reason": "telemetry_error"}
                if http_result == "timeout" else {"status": "missing", "reason": "http_403"})
    for attempt in report["attempts"]:
        assert attempt["status"] == "failed"
        assert attempt["collection"] == dict.fromkeys(PAIRS, expected)
    assert collection(report) == {pair: {**expected, "attempt_id": 2} for pair in PAIRS}
    assert report["execution"]["status"] == ("timeout" if http_result == "timeout" else "normal")


@pytest.mark.parametrize("method", ["start_attempt", "observation", "finish_attempt"])
@pytest.mark.parametrize("after_evidence", [False, True])
def test_telemetry_fallback_preserves_observed_pairs_and_marks_only_unresolved(
        harness, monkeypatch, method, after_evidence):
    cls = investing_report.InvestingReport
    original = getattr(cls, method)
    observe = cls.observation

    def fail(report, *args, **kwargs):
        if after_evidence:
            original(report, *args, **kwargs)
        if method == "start_attempt":
            # 부분 계측 뒤 실패: 유효값과 누락 판정을 모두 보존해야 한다.
            observe(report, args[0], "usd-krw", text="1350", rate=1350)
            observe(report, args[0], "jpy-krw", reason="selector_missing")
        raise RuntimeError("partial telemetry")

    monkeypatch.setattr(cls, method, fail)
    if method == "start_attempt":
        harness.http.return_value = response(status=403)
    else:
        harness.http.return_value = response({"usd-krw": "1350"})
    investing.crawl_and_save_investing_exchange_rates()
    report = finished(harness)
    assert report["format"] == "detail"
    assert all(e["format"] == "detail" for e in harness.events("investing_fx_evidence"))
    first = report["attempts"][0]["collection"]
    if method == "observation" and not after_evidence:
        assert first == dict.fromkeys(PAIRS, {"status": "unknown", "reason": "telemetry_error"})
    else:
        assert first["usd-krw"] == {"status": "valid", "reason": "validated", "normalized_rate": 1350}
        assert first["jpy-krw"] == {"status": "missing", "reason": "selector_missing"}
        assert first["eur-krw"] == {
            "status": "missing", "reason": "http_403" if method == "start_attempt" else "selector_missing"}


@pytest.mark.parametrize("second_result", ["timeout", "403"])
def test_lost_retry_start_preserves_prior_observations(harness, monkeypatch, second_result):
    original_start = investing_report.InvestingReport.start_attempt

    def fail_second_start(report, attempt_id):
        if attempt_id == 2:
            raise RuntimeError("retry telemetry")
        original_start(report, attempt_id)

    monkeypatch.setattr(investing_report.InvestingReport, "start_attempt", fail_second_start)
    harness.http.side_effect = [response({"usd-krw": "1350"}),
                               TimeoutError() if second_result == "timeout" else response(status=403)]
    harness.dxy.side_effect = RuntimeError("DXY after FX")
    investing.crawl_and_save_investing_exchange_rates()
    report = finished(harness)
    assert report["format"] == "detail"
    assert harness.http.call_count == 2
    harness.writer.assert_called_once()
    observed = collection(report)
    assert observed["usd-krw"] == {
        "status": "valid", "reason": "validated", "normalized_rate": 1350, "attempt_id": 1}
    first = report["attempts"][0]["collection"]
    assert first["jpy-krw"] == first["eur-krw"] == {
        "status": "missing", "reason": "selector_missing"}
    expected = ({"status": "unknown", "reason": "telemetry_error"}
                if second_result == "timeout" else {"status": "missing", "reason": "http_403"})
    assert report["attempts"][1]["collection"] == dict.fromkeys(PAIRS, expected)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit()])
def test_process_exit_after_compact_evidence_preserves_full_diagnostics(harness, error):
    harness.dxy.side_effect = error
    with pytest.raises(type(error)) as caught:
        investing.crawl_and_save_investing_exchange_rates()
    assert caught.value is error
    evidence, = harness.events("investing_fx_evidence")
    assert evidence["format"] == "compact"
    assert evidence["rates"]["jpy-krw"] == 900
    report = finished(harness)
    assert report["format"] == "detail"
    assert report["attempts"][0]["execution"]["error_type"] == type(error).__name__
    assert "collection" not in report["attempts"][1]
    harness.http.assert_called_once()


@pytest.mark.parametrize("retain_error_marker", [False, True])
def test_unknown_collection_cannot_become_compact_valid(retain_error_marker):
    logger = Mock()
    report = investing_report.InvestingReport(logger, PAIRS)
    report.session_state("open")
    report.start_attempt(1)
    for pair, rate in zip(PAIRS, (1350, 900, 1500)):
        report.observation(1, pair, text=str(rate), rate=rate)
    report.attempts[1]["collection"]["usd-krw"] = {
        "status": "unknown", "reason": "telemetry_error"}
    if retain_error_marker:
        report.telemetry_errors.append("observation")
    report.writer_started(1, dict.fromkeys(PAIRS, 1))
    report.writer_finished(1, count=3)
    report.emit("investing_fx_evidence", 1)
    report.finish_attempt(1)
    report.session_state("closed")
    report.finish()
    for call in logger.info.call_args_list:
        event = json.loads(call.args[0])
        assert event["format"] == "detail"
        assert "outcome" not in event and "rates" not in event
        assert collection(event)["usd-krw"] == {
            "status": "unknown", "reason": "telemetry_error", "attempt_id": 1}


@pytest.mark.parametrize("scenario", ["normal", "unchanged", "partial", "cooldown",
                                      "retry_success", "timeout", "dxy_then_timeout",
                                      "all_parse_failed", "writer_error", "dxy_retry_success"])
def test_log_volume(harness, monkeypatch, caplog, scenario):
    """실제 crawler 경로에서 UTF-8 message / app.log 외곽+개행까지 측정한다."""
    h = harness
    if scenario == "unchanged":
        h.writer.return_value = 0
    elif scenario == "partial":
        h.http.return_value = response({"usd-krw": NORMAL["usd-krw"]})
    elif scenario == "cooldown":
        monkeypatch.setattr(investing, "_cooldown_until", investing.time.monotonic() + 600)
    elif scenario == "retry_success":
        h.http.side_effect = [TimeoutError(), response()]
    elif scenario == "timeout":
        h.http.side_effect = TimeoutError()
    elif scenario == "dxy_then_timeout":
        h.dxy.side_effect = RuntimeError("DXY")
        h.http.side_effect = [response(), TimeoutError()]
    elif scenario == "all_parse_failed":
        h.http.return_value = response({})
    elif scenario == "writer_error":
        h.writer.side_effect = RuntimeError("writer")
    elif scenario == "dxy_retry_success":
        h.dxy.side_effect = [RuntimeError("DXY"), 1]
    investing.crawl_and_save_investing_exchange_rates()
    records = [record for record in caplog.records
               if record.name == investing.logger.name and record.getMessage().startswith('{')]
    formatter = CustomJsonFormatter()
    # 타임스탬프 소수점 자릿수만 고정. 운영과 같은 6자리·logger/function/line.
    for record in records:
        record.created = 1800000000.123456
    messages = [len(record.getMessage().encode("utf-8")) for record in records]
    lines = [len((formatter.format(record) + "\n").encode("utf-8")) for record in records]
    daily_mib = sum(lines) * (19 * 3600 // 10 + 5 * 3600 // 60) / 1024**2
    print(f"VOLUME {scenario}: message_bytes={messages} line_bytes={lines} "
          f"bytes_per_round={sum(lines)} MiB_per_day={daily_mib:.4f}")
    if scenario in ("normal", "unchanged"):
        assert len(records) == 3  # FX 증거를 생략해서 용량 시험을 통과하면 안 된다.
        assert sum(lines) <= 2000
        assert [e["format"] for e in h.events()] == ["lifecycle", "compact", "compact"]
    else:
        assert finished(h)["format"] == "detail"


@pytest.mark.parametrize("scenario", ["valid", "invalid", "parse_failed", "403", "cooldown",
                                      "timeout", "create_error", "close_error"])
def test_scheduler_stats_mapping_remains_legacy(harness, monkeypatch, scenario):
    h = harness
    stats = Mock()
    monkeypatch.setattr(scheduler, "crawler_stats", stats)
    if scenario == "invalid":
        h.http.return_value = response(dict(zip(PAIRS, ("nan", "inf", "99999"))))
    elif scenario == "parse_failed":
        h.http.return_value = response({})
    elif scenario == "403":
        h.http.return_value = response(status=403)
    elif scenario == "cooldown":
        monkeypatch.setattr(investing, "_cooldown_until", investing.time.monotonic() + 600)
    elif scenario == "timeout":
        h.http.side_effect = TimeoutError()
    elif scenario == "create_error":
        h.session.side_effect = RuntimeError()
    elif scenario == "close_error":
        h.db.close.side_effect = RuntimeError()
    scheduler.make_request_crawler_wrapper("investing", investing.crawl_and_save_investing_exchange_rates)()
    if scenario in ("create_error", "close_error"):
        stats.record_failure.assert_called_once_with("investing")
        stats.record_success.assert_not_called()
    else:
        stats.record_success.assert_called_once()
        assert stats.record_success.call_args.args[0] == "investing"
        stats.record_failure.assert_not_called()
    finished(h)
