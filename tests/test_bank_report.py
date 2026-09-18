"""bs·citi 보고 슬라이스 R1a — 합성 HTML 로 감지 로직과 **기존 동작 불변**을 잠근다.

⛔ 합성 fixture 는 실제 페이지의 의미("그 칸이 기준환율")를 주장하지 않는다. 판정은 R1a 계약대로
`unknown(v2_evidence_unconfirmed)` 이 기본이다(라벨 표기가 실응답 fixture 로 확인되기 전).
"""

import asyncio
import json
import logging
import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app.crawlers import bank_report, bs, citi


def page(html, status=200):
    return SimpleNamespace(status_code=status, text=html, raise_for_status=Mock())


def bs_html(rows):
    body = "".join(f"<tr><td>{label}</td><td>{value}</td></tr>" for label, value in rows)
    return ('<table id="resultTable"><thead><tr><th>통화</th><th>매매기준율</th></tr></thead>'
            f"<tbody>{body}</tbody></table>")


BS_NORMAL = [("미국 USD", "1,393.50"), ("일본 JPY 100", "942.64"), ("유럽 EUR", "1,641.12")]


def citi_first_html(items):
    lis = "".join(f"<li><div><div>{label}</div><div><span>{value}</span></div></div></li>"
                  for label, value in items)
    return f'<div id="content"><ul>{lis}</ul></div>'


CITI_NORMAL = [("미국(USD)", "1,393.50"), ("중국(CNY)", "195.72"),
               ("유럽(EUR)", "1,641.12"), ("일본(JPY)", "942.64")]


def citi_second_html(rows):
    body = "".join(f"<tr><td>{label}</td><td>{value}</td></tr>" for label, value in rows)
    return f'<div id="tab01"><table><tbody>{body}</tbody></table></div>'


@pytest.fixture
def harness(monkeypatch, caplog):
    def forbidden(*args, **kwargs):
        raise AssertionError("real I/O forbidden")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    for module in (bs, citi):
        caplog.set_level(logging.INFO, logger=module.logger.name)
    db = Mock()
    session = Mock(return_value=db)
    writer = Mock(return_value=3)
    http = Mock()
    reliable = Mock(return_value=False)
    for module in (bs, citi):
        monkeypatch.setattr(module, "SessionLocal", session)
        monkeypatch.setattr(module.requests, "get", http)
        monkeypatch.setattr(module, "is_mibank_rate_reliable", reliable)
    monkeypatch.setattr(bs.crud, "insert_bank_rates_into_db", writer)
    mibank_bs = Mock()
    mibank_citi = Mock()
    monkeypatch.setattr(bs, "_crawl_mibank_bs", mibank_bs)
    monkeypatch.setattr(citi, "_crawl_mibank_citi", mibank_citi)

    def events(module, name=None):
        parsed = [json.loads(r.getMessage()) for r in caplog.records
                  if r.name == module.logger.name and r.getMessage().startswith("{")]
        return [e for e in parsed if name is None or e["event"] == name]

    def plain_logs(module):
        return [r.getMessage() for r in caplog.records
                if r.name == module.logger.name and not r.getMessage().startswith("{")]

    return SimpleNamespace(db=db, session=session, writer=writer, http=http, reliable=reliable,
                           mibank_bs=mibank_bs, mibank_citi=mibank_citi, events=events,
                           plain_logs=plain_logs)


def finished(h, module):
    [event] = h.events(module, "bank_round_finished")
    [started] = h.events(module, "bank_round_started")
    assert started["round_id"] == event["round_id"]
    assert event["validity_contract"] == "bank_v2_evidence/1"
    return event


def attempt(event, path):
    return next(a for a in event["attempts"] if a["path"] == path)


# ── bs 공식 ────────────────────────────────────────────────────────────────


def test_bs_normal_round_keeps_writer_input_and_reports_unconfirmed(harness):
    harness.http.return_value = page(bs_html(BS_NORMAL))
    assert bs.crawl_and_save_bs_bank_exchange_rates() is None
    harness.writer.assert_called_once_with(
        db=harness.db, current_rates={"usd-krw": 1393.5, "jpy-krw": 942.64, "eur-krw": 1641.12},
        bank_name="bs")
    event = finished(harness, bs)
    official = attempt(event, "official_primary")
    assert official["status"] == "succeeded"
    for pair in ("usd-krw", "jpy-krw", "eur-krw"):
        judged = official["collection"][pair]
        assert (judged["status"], judged["reason"]) == ("unknown", "v2_evidence_unconfirmed")
        assert event["summary"]["collection"][pair]["path"] == "official_primary"
        assert event["summary"]["writing"][pair]["reason"] == "per_currency_write_unverified"
    usd = next(o for o in official["observations"] if o["pair"] == "usd-krw")
    assert "USD" in usd["label_candidates"]["row_text"]["text"]
    assert "매매기준율" in usd["label_candidates"]["header_text"]["text"]
    assert usd["label_candidates"]["cell_index"] == 1
    assert attempt(event, "mibank")["collection"]["usd-krw"] == {
        "status": "not_attempted", "reason": "previous_attempt_succeeded"}
    [call] = event["writer_calls"]
    assert (call["path"], call["termination"], call["returned_count"],
            call["guard_decision"]) == ("official_primary", "returned", 3, "not_instrumented")
    assert event["summary"]["final_db"] == "not_checked"
    assert event["execution"]["status"] == "normal"


def test_bs_partial_selector_miss_and_parse_error(harness):
    harness.http.return_value = page(bs_html([("미국 USD", "1,393.50"), ("일본 JPY", "N/A")]))
    bs.crawl_and_save_bs_bank_exchange_rates()
    harness.writer.assert_called_once_with(
        db=harness.db, current_rates={"usd-krw": 1393.5}, bank_name="bs")
    official = attempt(finished(harness, bs), "official_primary")
    assert official["collection"]["jpy-krw"]["reason"] == "no_value"
    assert official["collection"]["jpy-krw"]["detail"] == "parse_error"
    assert official["collection"]["eur-krw"]["detail"] == "selector_miss"
    assert official["collection"]["usd-krw"]["status"] == "unknown"


def test_bs_nan_is_rejected_in_report_but_passed_to_writer_unchanged(harness):
    harness.http.return_value = page(bs_html([("미국 USD", "NaN"), ("일본 JPY", "942"),
                                              ("유럽 EUR", "1,641")]))
    bs.crawl_and_save_bs_bank_exchange_rates()
    rates = harness.writer.call_args.kwargs["current_rates"]
    assert math.isnan(rates["usd-krw"]), "기존 동작: NaN 도 writer 로 간다"
    judged = attempt(finished(harness, bs), "official_primary")["collection"]["usd-krw"]
    assert (judged["status"], judged["reason"], judged["detail"]) == (
        "missing", "validation_rejected", "non_finite")


def test_bs_all_missing_then_policy_skip_is_swallowed_and_reported(harness):
    harness.http.return_value = page(bs_html([]))
    assert bs.crawl_and_save_bs_bank_exchange_rates() is None, "기존 동작: 총실패를 삼킨다"
    harness.writer.assert_not_called()
    event = finished(harness, bs)
    official = attempt(event, "official_primary")
    assert official["status"] == "failed"
    assert all(official["collection"][p]["detail"] == "selector_miss"
               for p in ("usd-krw", "jpy-krw", "eur-krw"))
    mib = attempt(event, "mibank")
    assert (mib["status"], mib["collection"]["usd-krw"]) == (
        "policy_skipped", {"status": "not_attempted", "reason": "mibank_untrusted_window"})
    assert event["summary"]["collection"]["usd-krw"]["status"] == "missing"
    assert event["summary"]["writing"]["usd-krw"]["status"] == "not_attempted"
    assert event["execution"]["status"] == "normal", "정상 반환은 사실 — 수집 성공이 아니다"


def test_bs_fetch_failure_then_mibank_runs_uninstrumented(harness):
    harness.http.side_effect = requests.ConnectionError("down")
    harness.reliable.return_value = True
    rates = {"usd-krw": 1390.0, "jpy-krw": 940.0, "eur-krw": 1640.0}
    harness.mibank_bs.return_value = (rates, {"hard_fail": False, "soft_fail": False,
                                              "details": []})
    bs.crawl_and_save_bs_bank_exchange_rates()
    harness.writer.assert_called_once_with(db=harness.db, current_rates=rates, bank_name="bs")
    event = finished(harness, bs)
    official = attempt(event, "official_primary")
    assert official["collection"]["usd-krw"]["detail"] == "path_failed"
    mib = attempt(event, "mibank")
    assert mib["status"] == "succeeded"
    assert mib["collection"]["usd-krw"] == {"status": "unknown", "reason": "not_instrumented"}
    [call] = event["writer_calls"]
    assert call["path"] == "mibank" and call["termination"] == "returned"


def test_bs_mibank_hard_fail_does_not_call_writer(harness):
    harness.http.side_effect = requests.ConnectionError("down")
    harness.reliable.return_value = True
    harness.mibank_bs.return_value = ({"usd-krw": 1.0}, {"hard_fail": True, "soft_fail": True,
                                                        "details": []})
    bs.crawl_and_save_bs_bank_exchange_rates()
    harness.writer.assert_not_called()
    event = finished(harness, bs)
    assert event["writer_calls"] == []
    assert attempt(event, "mibank")["collection"]["usd-krw"]["reason"] == "not_instrumented"


def test_bs_writer_exception_keeps_official_observations(harness):
    harness.http.return_value = page(bs_html(BS_NORMAL))
    harness.writer.side_effect = RuntimeError("db down")
    bs.crawl_and_save_bs_bank_exchange_rates()
    event = finished(harness, bs)
    official = attempt(event, "official_primary")
    assert official["status"] == "failed"
    assert official["collection"]["usd-krw"]["reason"] == "v2_evidence_unconfirmed"
    [call] = event["writer_calls"]
    assert (call["termination"], call["error_type"]) == ("raised", "RuntimeError")
    assert event["summary"]["collection"]["usd-krw"]["path"] == "official_primary"


def test_routine_without_observer_emits_nothing(harness):
    harness.http.return_value = page(bs_html(BS_NORMAL))
    assert bs.crawl_and_save_routine(bs.BS_BANK_URL, bs.BS_BANK_SELECTORS, harness.db) == 3
    assert harness.events(bs) == []


# ── citi ───────────────────────────────────────────────────────────────────


def test_citi_primary_text_match_records_item_and_code(harness):
    harness.http.return_value = page(citi_first_html(CITI_NORMAL))
    citi.crawl_and_save_citi_bank_exchange_rates()
    harness.writer.assert_called_once_with(
        db=harness.db,
        current_rates={"usd-krw": 1393.5, "eur-krw": 1641.12, "jpy-krw": 942.64},
        bank_name="citi")
    event = finished(harness, citi)
    primary = attempt(event, "official_primary")
    usd = next(o for o in primary["observations"] if o["pair"] == "usd-krw")
    assert (usd["item_key"], usd["matched_code"]) == ("1st", "USD")
    assert "미국(USD)" in usd["label_candidates"]["item_text"]["text"]
    assert primary["collection"]["usd-krw"]["reason"] == "v2_evidence_unconfirmed"
    for path in ("official_secondary", "mibank"):
        assert attempt(event, path)["collection"]["usd-krw"]["status"] == "not_attempted"


def test_citi_one_item_with_two_codes_is_attribution_conflict(harness):
    items = [("USD EUR 혼합", "1,000.00"), ("일본(JPY)", "942.64")]
    harness.http.return_value = page(citi_first_html(items))
    citi.crawl_and_save_citi_bank_exchange_rates()
    rates = harness.writer.call_args.kwargs["current_rates"]
    assert rates["usd-krw"] == rates["eur-krw"] == 1000.0, "기존 동작: 두 통화에 같은 값"
    primary = attempt(finished(harness, citi), "official_primary")
    for pair in ("usd-krw", "eur-krw"):
        assert primary["collection"][pair]["detail"] == "attribution_conflict"
    assert primary["collection"]["jpy-krw"]["status"] == "unknown"


def test_citi_same_code_in_two_items_is_conflict_and_last_wins(harness):
    items = [("미국(USD)", "1,393.50"), ("미국(USD) 재게시", "1,400.00")]
    harness.http.return_value = page(citi_first_html(items))
    citi.crawl_and_save_citi_bank_exchange_rates()
    assert harness.writer.call_args.kwargs["current_rates"] == {"usd-krw": 1400.0}
    primary = attempt(finished(harness, citi), "official_primary")
    assert primary["collection"]["usd-krw"]["detail"] == "attribution_conflict"
    assert primary["collection"]["jpy-krw"]["detail"] == "not_matched"


def test_citi_primary_failure_falls_back_to_secondary(harness):
    harness.http.side_effect = [page(citi_first_html([])),
                                page(citi_second_html(BS_NORMAL))]
    citi.crawl_and_save_citi_bank_exchange_rates()
    event = finished(harness, citi)
    assert attempt(event, "official_primary")["status"] == "failed"
    secondary = attempt(event, "official_secondary")
    assert secondary["status"] == "succeeded"
    assert secondary["collection"]["usd-krw"]["reason"] == "v2_evidence_unconfirmed"
    usd = next(o for o in secondary["observations"] if o["pair"] == "usd-krw")
    assert "USD" in usd["label_candidates"]["row_text"]["text"]
    assert event["summary"]["collection"]["usd-krw"]["path"] == "official_secondary"
    assert attempt(event, "mibank")["collection"]["usd-krw"]["reason"] == \
        "previous_attempt_succeeded"


# ── 경계: 관측 실패 격리·예외 전파·잘림 ────────────────────────────────────


def test_observer_failure_changes_nothing_but_marks_evidence_incomplete(harness, monkeypatch):
    harness.http.return_value = page(bs_html(BS_NORMAL))
    monkeypatch.setattr(bank_report.BankReport, "observed",
                        Mock(side_effect=RuntimeError("probe")))
    assert bs.crawl_and_save_bs_bank_exchange_rates() is None
    harness.writer.assert_called_once()
    official = attempt(finished(harness, bs), "official_primary")
    judged = official["collection"]["usd-krw"]
    assert (judged["status"], judged["reason"]) == ("unknown", "evidence_incomplete")


def test_report_init_failure_keeps_behavior_and_emits_nothing(harness, monkeypatch):
    harness.http.return_value = page(bs_html(BS_NORMAL))
    monkeypatch.setattr(bank_report, "BankReport", Mock(side_effect=RuntimeError("init")))
    assert bs.crawl_and_save_bs_bank_exchange_rates() is None
    harness.writer.assert_called_once()
    assert harness.events(bs) == []


def test_plain_logs_are_identical_with_and_without_report(harness, monkeypatch, caplog):
    harness.http.return_value = page(bs_html([("미국 USD", "1,393.50"), ("일본 JPY", "N/A")]))
    bs.crawl_and_save_bs_bank_exchange_rates()
    with_report = harness.plain_logs(bs)
    caplog.clear()
    monkeypatch.setattr(bank_report, "BankReport", Mock(side_effect=RuntimeError("off")))
    bs.crawl_and_save_bs_bank_exchange_rates()
    assert harness.plain_logs(bs) == with_report


@pytest.mark.parametrize("phase", ["session", "close"])
def test_session_exceptions_propagate_unchanged_and_are_reported(harness, phase):
    boom = RuntimeError(phase)
    if phase == "session":
        harness.session.side_effect = boom
    else:
        harness.http.return_value = page(bs_html(BS_NORMAL))
        harness.db.close.side_effect = boom
    with pytest.raises(RuntimeError) as raised:
        bs.crawl_and_save_bs_bank_exchange_rates()
    assert raised.value is boom
    event = finished(harness, bs)
    assert event["execution"]["exception_propagated"] is True
    assert event["execution"]["status"] == "abnormal"


def test_start_attempt_telemetry_loss_is_not_reported_as_not_attempted(harness, monkeypatch):
    harness.http.return_value = page(bs_html(BS_NORMAL))
    monkeypatch.setattr(bank_report.BankReport, "start_attempt",
                        Mock(side_effect=RuntimeError("probe")))
    monkeypatch.setattr(bank_report.BankReport, "finish_attempt",
                        Mock(side_effect=RuntimeError("probe")))
    bs.crawl_and_save_bs_bank_exchange_rates()
    official = attempt(finished(harness, bs), "official_primary")
    assert official["status"] == "unknown"
    assert official["collection"]["usd-krw"]["status"] == "unknown"


def test_truncated_observations_do_not_confirm_absence_of_conflict(harness, monkeypatch):
    monkeypatch.setattr(bank_report, "MAX_OBSERVATIONS_PER_PAIR", 1)
    items = [("미국(USD)", "1,393.50"), ("미국(USD) 재게시", "1,400.00")]
    harness.http.return_value = page(citi_first_html(items))
    citi.crawl_and_save_citi_bank_exchange_rates()
    event = finished(harness, citi)
    assert event["truncated"] is True
    judged = attempt(event, "official_primary")["collection"]["usd-krw"]
    assert (judged["status"], judged["reason"]) == ("unknown", "evidence_incomplete")


def test_snippet_bounds_and_marks_truncation():
    long_text = "가" * 400
    snippet = bank_report._snippet(long_text)
    assert snippet["truncated"] is True
    assert snippet["original_length"] == 400
    assert len(snippet["text"].encode("utf-8")) <= bank_report.SNIPPET_MAX_BYTES
    assert len(snippet["text"]) <= bank_report.SNIPPET_MAX_CHARS


def test_summary_prefers_later_unconfirmed_value_over_earlier_confirmed_miss(harness):
    """공식이 값을 못 얻고 MIBANK 가 값을 얻었다(근거 미확인) — 회차는 `누락` 이 아니라 `확인 불가`."""
    harness.http.side_effect = requests.ConnectionError("down")
    harness.reliable.return_value = True
    harness.mibank_bs.return_value = ({"usd-krw": 1390.0}, {"hard_fail": False, "soft_fail": False,
                                                           "details": []})
    bs.crawl_and_save_bs_bank_exchange_rates()
    summary = finished(harness, bs)["summary"]["collection"]["usd-krw"]
    assert (summary["status"], summary["path"]) == ("unknown", "mibank")


@pytest.mark.parametrize("where", ["official_http", "mibank"])
def test_cancellation_ends_the_active_attempt_and_propagates(harness, where):
    """취소는 폴백이 삼키지 않는다(기존 동작) — 진행 중 시도를 `running` 으로 남기지 않는다."""
    cancelled = asyncio.CancelledError()
    if where == "official_http":
        harness.http.side_effect = cancelled
        active = "official_primary"
    else:
        harness.http.side_effect = requests.ConnectionError("down")
        harness.reliable.return_value = True
        harness.mibank_bs.side_effect = cancelled
        active = "mibank"
    with pytest.raises(asyncio.CancelledError) as raised:
        bs.crawl_and_save_bs_bank_exchange_rates()
    assert raised.value is cancelled
    event = finished(harness, bs)
    ended = attempt(event, active)
    assert (ended["status"], ended["error_type"]) == ("failed", "CancelledError")
    assert ended["execution"]["status"] == "cancelled"
    assert event["execution"]["status"] == "cancelled"
    assert event["execution"]["exception_propagated"] is True
    harness.writer.assert_not_called()


def test_citi_mibank_soft_fail_calls_writer_and_records_it(harness):
    harness.http.side_effect = [page(citi_first_html([])), page(citi_second_html([]))]
    harness.reliable.return_value = True
    rates = {"usd-krw": 1390.0, "jpy-krw": 940.0, "eur-krw": 1640.0}
    harness.mibank_citi.return_value = (rates, {"hard_fail": False, "soft_fail": True,
                                                "details": []})
    citi.crawl_and_save_citi_bank_exchange_rates()
    harness.writer.assert_called_once_with(db=harness.db, current_rates=rates, bank_name="citi")
    event = finished(harness, citi)
    [call] = event["writer_calls"]
    assert call["path"] == "mibank" and call["input_pairs"] == sorted(rates)
    assert event["summary"]["writing"]["jpy-krw"]["writer_call_ids"] == [call["writer_call_id"]]


def test_summary_takes_the_latest_attempt_among_equal_statuses(harness):
    """두 공식 경로가 모두 확정 누락이면 가장 최근 경로의 증거를 요약에 쓴다."""
    harness.http.side_effect = [page(citi_first_html([])), page(citi_second_html([]))]
    citi.crawl_and_save_citi_bank_exchange_rates()
    event = finished(harness, citi)
    assert attempt(event, "official_primary")["collection"]["usd-krw"]["status"] == "missing"
    summary = event["summary"]["collection"]["usd-krw"]
    assert (summary["status"], summary["path"], summary["detail"]) == (
        "missing", "official_secondary", "selector_miss")


def test_label_candidate_failure_keeps_the_observation(harness, monkeypatch):
    harness.http.return_value = page(bs_html(BS_NORMAL))
    monkeypatch.setattr(bank_report, "_label_candidates", Mock(side_effect=RuntimeError("dom")))
    bs.crawl_and_save_bs_bank_exchange_rates()
    official = attempt(finished(harness, bs), "official_primary")
    usd = next(o for o in official["observations"] if o["pair"] == "usd-krw")
    assert usd["label_candidates_error"] == "RuntimeError" and usd["rate"] == 1393.5
    assert official["collection"]["usd-krw"]["reason"] == "v2_evidence_unconfirmed"


def test_lost_later_observation_does_not_confirm_earlier_miss(harness, monkeypatch):
    """첫 USD 항목은 파싱 실패, 두 번째 USD 관측은 계측이 유실 — writer 는 1400 을 받았다."""
    items = [("미국(USD)", "N/A"), ("미국(USD) 재게시", "1,400.00")]
    harness.http.return_value = page(citi_first_html(items))
    monkeypatch.setattr(bank_report.BankReport, "observed", Mock(side_effect=RuntimeError("lost")))
    citi.crawl_and_save_citi_bank_exchange_rates()
    assert harness.writer.call_args.kwargs["current_rates"] == {"usd-krw": 1400.0}
    event = finished(harness, citi)
    judged = attempt(event, "official_primary")["collection"]["usd-krw"]
    assert (judged["status"], judged["reason"]) == ("unknown", "evidence_incomplete")
    assert event["summary"]["collection"]["usd-krw"]["status"] == "unknown"


def test_lifecycle_telemetry_loss_does_not_erase_a_confirmed_violation(harness, monkeypatch):
    harness.http.return_value = page(bs_html([("미국 USD", "NaN"), ("일본 JPY", "942"),
                                              ("유럽 EUR", "1,641")]))
    monkeypatch.setattr(bank_report.BankReport, "start_attempt",
                        Mock(side_effect=RuntimeError("probe")))
    monkeypatch.setattr(bank_report.BankReport, "finish_attempt",
                        Mock(side_effect=RuntimeError("probe")))
    bs.crawl_and_save_bs_bank_exchange_rates()
    judged = attempt(finished(harness, bs), "official_primary")["collection"]["usd-krw"]
    assert (judged["status"], judged["reason"], judged["detail"]) == (
        "missing", "validation_rejected", "non_finite")


def test_lost_attempt_end_is_not_blamed_on_the_round_exception(harness, monkeypatch):
    """공식 경로 종료 기록만 유실 + MIBANK 정상 + close 실패 — 회차 예외를 공식 경로에 붙이지 않는다."""
    harness.http.side_effect = requests.ConnectionError("down")
    harness.reliable.return_value = True
    harness.mibank_bs.return_value = ({"usd-krw": 1390.0}, {"hard_fail": False, "soft_fail": False,
                                                           "details": []})
    real_finish = bank_report.BankReport.finish_attempt

    def lose_official_end(self, path, error=None):
        if path == "official_primary":
            raise RuntimeError("probe")
        return real_finish(self, path, error=error)

    monkeypatch.setattr(bank_report.BankReport, "finish_attempt", lose_official_end)
    closing = RuntimeError("close")
    harness.db.close.side_effect = closing
    with pytest.raises(RuntimeError) as raised:
        bs.crawl_and_save_bs_bank_exchange_rates()
    assert raised.value is closing
    event = finished(harness, bs)
    official = attempt(event, "official_primary")
    assert (official["status"], official["reason"]) == ("unknown", "attempt_end_unrecorded")
    assert "error_type" not in official
    assert attempt(event, "mibank")["status"] == "succeeded"
    assert event["execution"]["error_type"] == "RuntimeError"


def test_cancellation_then_close_failure_keeps_each_cause(harness):
    cancelled = asyncio.CancelledError()
    closing = RuntimeError("close")
    harness.http.side_effect = cancelled
    harness.db.close.side_effect = closing
    with pytest.raises(RuntimeError) as raised:
        bs.crawl_and_save_bs_bank_exchange_rates()
    assert raised.value is closing and raised.value.__context__ is cancelled
    event = finished(harness, bs)
    official = attempt(event, "official_primary")
    assert (official["error_type"], official["execution"]["status"]) == ("CancelledError", "cancelled")
    assert event["execution"]["error_type"] == "RuntimeError"


def test_citi_secondary_cancellation_is_bound_to_the_secondary_path(harness):
    cancelled = asyncio.CancelledError()
    harness.http.side_effect = [page(citi_first_html([])), cancelled]
    with pytest.raises(asyncio.CancelledError):
        citi.crawl_and_save_citi_bank_exchange_rates()
    event = finished(harness, citi)
    assert attempt(event, "official_primary")["error_type"] == "Exception"
    secondary = attempt(event, "official_secondary")
    assert (secondary["status"], secondary["error_type"]) == ("failed", "CancelledError")
    assert attempt(event, "mibank")["collection"]["usd-krw"]["status"] == "not_attempted"


@pytest.mark.parametrize("where", ["primary", "mibank"])
def test_citi_cancellation_is_bound_to_the_active_path(harness, where):
    cancelled = asyncio.CancelledError()
    if where == "primary":
        harness.http.side_effect = cancelled
        active = "official_primary"
    else:
        harness.http.side_effect = [page(citi_first_html([])), page(citi_second_html([]))]
        harness.reliable.return_value = True
        harness.mibank_citi.side_effect = cancelled
        active = "mibank"
    with pytest.raises(asyncio.CancelledError):
        citi.crawl_and_save_citi_bank_exchange_rates()
    ended = attempt(finished(harness, citi), active)
    assert (ended["status"], ended["error_type"]) == ("failed", "CancelledError")


def test_mibank_run_with_lost_end_record_is_not_reported_as_not_attempted(harness, monkeypatch):
    """시작 기록이 있어야 종료 기록이 유실돼도 '시도했음' 이 남는다."""
    harness.http.side_effect = requests.ConnectionError("down")
    harness.reliable.return_value = True
    harness.mibank_bs.return_value = ({"usd-krw": 1390.0}, {"hard_fail": False, "soft_fail": False,
                                                           "details": []})
    real_finish = bank_report.BankReport.finish_attempt

    def lose_mibank_end(self, path, error=None):
        if path == "mibank":
            raise RuntimeError("probe")
        return real_finish(self, path, error=error)

    monkeypatch.setattr(bank_report.BankReport, "finish_attempt", lose_mibank_end)
    bs.crawl_and_save_bs_bank_exchange_rates()
    mib = attempt(finished(harness, bs), "mibank")
    assert (mib["status"], mib["reason"]) == ("unknown", "attempt_end_unrecorded")
    assert mib["collection"]["usd-krw"]["status"] == "unknown"
