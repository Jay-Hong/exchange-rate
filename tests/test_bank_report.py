"""bs·citi 보고 슬라이스 R1a — 합성 HTML 로 감지 로직과 **기존 동작 불변**을 잠근다.

⛔ 합성 fixture 는 실제 페이지의 의미("그 칸이 기준환율")를 주장하지 않는다. 판정은 R1a 계약대로
`unknown(v2_evidence_unconfirmed)` 이 기본이다(라벨 표기가 실응답 fixture 로 확인되기 전).
"""

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app.crawlers import bank_report, bs, citi, utils

# 하네스가 `_crawl_mibank_*` 를 Mock 으로 바꾸기 전에 원본을 잡아 둔다(R1b 는 실제 MIBANK 경로를 돈다).
REAL_MIBANK = {bs: bs._crawl_mibank_bs, citi: citi._crawl_mibank_citi}


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


MIBANK_HEADER = ("통화", "매매기준율", "기준환율")


def mibank_html(rows, header=MIBANK_HEADER):
    thead = ("<thead><tr>" + "".join(f"<th>{h}</th>" for h in header) + "</tr></thead>"
             if header is not None else "")
    return (f'<div class="box_contents1"><table>{thead}<tbody>{"".join(rows)}</tbody>'
            "</table></div>")


def mrow(code, cells, via="href"):
    """합성 MIBANK 행 — 첫 칸이 통화 식별, 나머지가 값 칸. 실제 페이지 의미를 주장하지 않는다."""
    if via == "href":
        ident = f'<a href="/bank/detail?currency={code.lower()}">{code}</a>'
    else:
        ident = f'<img src="/img/flag_{code.lower()}_s.png">'
    return f"<tr><td>{ident}</td>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


MIBANK_NORMAL = [mrow("USD", ["1,380.00", "1,390.00"]), mrow("CNY", ["190.00", "191.00"]),
                 mrow("JPY", ["930.00", "940.00"], via="flag"), mrow("EUR", ["1,630.00", "1,640.00"])]
MIBANK_RATES = {"usd-krw": 1390.0, "jpy-krw": 940.0, "eur-krw": 1640.0}
NOW = datetime(2026, 9, 18, 3, 0)


def prior(rates=MIBANK_RATES, minutes=5):
    return {pair: {"rate": rate, "timestamp": NOW - timedelta(minutes=minutes)}
            for pair, rate in rates.items()}


@pytest.fixture
def real_mibank(harness, monkeypatch):
    """공식 경로는 실패시키고 실제 `_crawl_mibank_*` → `crawl_mibank_rates` 를 돈다."""
    for module in (bs, citi):
        monkeypatch.setattr(module, f"_crawl_mibank_{module.BANK_NAME}", REAL_MIBANK[module])
    harness.reliable.return_value = True
    last = Mock(return_value=prior())
    monkeypatch.setattr(bs.crud, "get_last_bank_rates_with_ts", last)
    monkeypatch.setattr(bs.models, "get_utc_now", Mock(return_value=NOW))

    def serve(module, html):
        official = [requests.ConnectionError("down")]
        if module is citi:
            official.append(requests.ConnectionError("down"))
        harness.http.side_effect = official + [page(html)]

    harness.last = last
    harness.serve = serve
    return harness


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


def test_bs_fetch_failure_then_mibank_rows_are_observed(real_mibank):
    h = real_mibank
    h.serve(bs, mibank_html(MIBANK_NORMAL))
    bs.crawl_and_save_bs_bank_exchange_rates()
    h.writer.assert_called_once_with(db=h.db, current_rates=MIBANK_RATES, bank_name="bs")
    event = finished(h, bs)
    assert attempt(event, "official_primary")["collection"]["usd-krw"]["detail"] == "path_failed"
    mib = attempt(event, "mibank")
    assert mib["status"] == "succeeded" and mib["loop_completed"] is True
    for pair in MIBANK_RATES:
        judged = mib["collection"][pair]
        # 통화 코드 근거는 통화 축만 채운다 — 필드·단위 근거가 없으니 유효로 올리지 않는다.
        assert (judged["status"], judged["reason"]) == ("unknown", "v2_evidence_unconfirmed")
        assert event["summary"]["collection"][pair]["path"] == "mibank"
    usd = next(o for o in mib["observations"] if o["pair"] == "usd-krw")
    assert (usd["code_basis"], usd["matched_code"], usd["item_key"]) == ("explicit_code_param", "USD", 0)
    assert usd["value_basis"] == {"branch": "header_index", "column_index": 2,
                                  "row_cell_count": 3, "used_counter_span": False}
    assert usd["label_candidates"]["row_cell_count"] == 3
    jpy = next(o for o in mib["observations"] if o["pair"] == "jpy-krw")
    assert jpy["code_basis"] == "flag_filename"
    assert mib["structure"]["column_basis"] == "label_found"
    assert mib["structure"]["header_has_span"] is False
    assert mib["outside_required_codes"] == {
        "codes": ["CNY"], "basis_counts": {"explicit_code_param": 1}, "truncated": False}
    assert not any(o["pair"] == "cny-krw" for o in mib["observations"]), "필수 밖 코드는 값을 읽지 않는다"
    assert mib["ops_range_check"] == {"termination": "returned", "error_type": None,
                                      "input_pairs": sorted(MIBANK_RATES), "non_finite_input_pairs": []}
    assert mib["deviation"]["hard_fail"] is False
    assert mib["deviation"]["pairs"]["usd-krw"]["compared"] is True
    assert mib["adoption"] == {"decision": "submitted", "reason": "deviation_not_hard_fail"}
    [call] = event["writer_calls"]
    assert call["path"] == "mibank" and call["termination"] == "returned"


def test_bs_mibank_hard_fail_is_withheld_before_writer(real_mibank):
    h = real_mibank
    h.last.return_value = prior({"usd-krw": 1000.0, "jpy-krw": 940.0, "eur-krw": 1640.0})
    h.serve(bs, mibank_html(MIBANK_NORMAL))
    bs.crawl_and_save_bs_bank_exchange_rates()
    h.writer.assert_not_called()
    event = finished(h, bs)
    assert event["writer_calls"] == []
    mib = attempt(event, "mibank")
    assert mib["deviation"]["hard_fail"] is True
    assert mib["adoption"] == {"decision": "withheld", "reason": "deviation_hard_fail"}
    assert mib["collection"]["usd-krw"]["reason"] == "v2_evidence_unconfirmed", "편차는 판정 계약 밖"
    writing = event["summary"]["writing"]["usd-krw"]
    assert (writing["status"], writing["reason"], writing["detail"], writing["path"]) == (
        "not_attempted", "withheld_before_writer", "deviation_hard_fail", "mibank")


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


# ── R1b: MIBANK 관측자 ───────────────────────────────────────────────────────


def _mibank_both_ways(monkeypatch, html, observer):
    """같은 응답으로 관측자 없음/있음을 각각 돌려 (반환 또는 예외) 를 돌려준다."""
    outcomes = []
    for obs in (None, observer):
        monkeypatch.setattr(utils.requests, "get", Mock(return_value=page(html)))
        try:
            outcomes.append(("returned", utils.crawl_mibank_rates("u", "bs", observer=obs)))
        except Exception as error:  # noqa: BLE001 - 동작 비교가 목적이다
            outcomes.append(("raised", type(error), str(error)))
    return outcomes


def _observer(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=bs.logger.name)
    report = bank_report.BankReport(bs.logger, "bs", ("usd-krw", "jpy-krw", "eur-krw"),
                                    (bank_report.MIBANK,))
    observer = bank_report.PathObserver(report, bank_report.MIBANK)
    observer.start()
    return report, observer


def test_header_index_cell_dash_does_not_fall_back_to_other_numeric_cell(monkeypatch, caplog):
    """① 헤더 인덱스 칸이 `-` 면 폴백하지 않고 None — 옆의 숫자 칸(counter 클래스)을 쓰지 않는다."""
    usd = ('<tr><td><a href="?currency=usd">USD</a></td>'
           '<td class="right counter rollsty01">1,385.00</td><td>-</td></tr>')
    html = mibank_html([usd, mrow("JPY", ["1", "940.00"]), mrow("EUR", ["1", "1,640.00"])])
    report, observer = _observer(monkeypatch, caplog)
    without, with_obs = _mibank_both_ways(monkeypatch, html, observer)
    assert without == with_obs
    assert without[0] == "raised" and without[1] is RuntimeError and "usd-krw" in without[2]
    attempt_ = report.attempts[bank_report.MIBANK]
    [miss] = [m for m in attempt_["misses"] if m["pair"] == "usd-krw"]
    assert miss["reason"] == "empty_value"
    assert miss["value_basis"]["branch"] == "header_index"
    assert miss["value_basis"]["column_index"] == 2
    assert miss["label_candidates"]["row_cell_count"] == 3


@pytest.mark.parametrize(("header", "expected_basis", "reason"), [
    (None, "header_row_absent", "column_index_unresolved"),
    (("통화", "매매기준율", "송금"), "label_not_found", "column_index_unresolved"),
])
def test_fallback_records_actual_selector_and_last_element(monkeypatch, caplog, header,
                                                           expected_basis, reason):
    rows = [('<tr><td><a href="?currency=usd">USD</a></td>'
             '<td class="right counter rollsty01">1,380.00</td>'
             '<td class="right counter rollsty01">1,390.00</td></tr>'),
            ('<tr><td><a href="?currency=jpy">JPY</a></td><td><span class="counter">940.00</span></td></tr>'),
            ('<tr><td><a href="?currency=eur">EUR</a></td><td><span class="counter">1,640.00</span></td></tr>')]
    html = mibank_html(rows, header=header)
    report, observer = _observer(monkeypatch, caplog)
    without, with_obs = _mibank_both_ways(monkeypatch, html, observer)
    assert without == with_obs == ("returned", MIBANK_RATES)
    attempt_ = report.attempts[bank_report.MIBANK]
    assert attempt_["structure"]["column_basis"] == expected_basis
    usd = next(o for o in attempt_["observations"] if o["pair"] == "usd-krw")
    assert usd["value_basis"] == {"branch": "fallback", "reason": reason,
                                  "selector": "td.right.counter.rollsty01", "matched_count": 2}
    jpy = next(o for o in attempt_["observations"] if o["pair"] == "jpy-krw")
    assert jpy["value_basis"]["selector"] == "span.counter"


def test_header_index_beyond_row_cells_falls_back_with_reason(monkeypatch, caplog):
    rows = [mrow("USD", ["1,390.00"]).replace("<td>1,390.00</td>",
                                               '<td><span class="counter">1,390.00</span></td>'),
            mrow("JPY", ["1", "940.00"]), mrow("EUR", ["1", "1,640.00"])]
    report, observer = _observer(monkeypatch, caplog)
    without, with_obs = _mibank_both_ways(monkeypatch, mibank_html(rows), observer)
    assert without == with_obs == ("returned", MIBANK_RATES)
    usd = next(o for o in report.attempts[bank_report.MIBANK]["observations"] if o["pair"] == "usd-krw")
    assert (usd["value_basis"]["branch"], usd["value_basis"]["reason"]) == (
        "fallback", "row_cells_insufficient")


def test_mid_row_parse_failure_propagates_and_is_bound_to_its_row(real_mibank):
    h = real_mibank
    rows = [mrow("USD", ["1", "1,390.00"]), mrow("JPY", ["1", "N/A"]), mrow("EUR", ["1", "1,640.00"])]
    h.serve(bs, mibank_html(rows))
    bs.crawl_and_save_bs_bank_exchange_rates()
    h.writer.assert_not_called()
    event = finished(h, bs)
    mib = attempt(event, "mibank")
    assert (mib["status"], mib["error_type"]) == ("failed", "ValueError")
    assert mib["loop_completed"] is False
    [miss] = mib["misses"]
    assert (miss["pair"], miss["reason"], miss["item_key"]) == ("jpy-krw", "parse_error", 1)
    assert miss["rate_text"]["text"] == "N/A"
    assert "JPY" in miss["label_candidates"]["row_text"]["text"]
    assert mib["collection"]["usd-krw"]["reason"] == "v2_evidence_unconfirmed", "앞 행 관측은 남는다"
    assert mib["collection"]["jpy-krw"]["detail"] == "parse_error"
    assert mib["collection"]["eur-krw"]["detail"] == "path_failed", "뒤 행은 읽지 않았다"
    assert "ops_range_check" not in mib and "deviation" not in mib


def test_mid_row_parse_failure_same_exception_without_observer(monkeypatch, caplog):
    rows = [mrow("USD", ["1", "1,390.00"]), mrow("JPY", ["1", "N/A"]), mrow("EUR", ["1", "1,640.00"])]
    report, observer = _observer(monkeypatch, caplog)
    without, with_obs = _mibank_both_ways(monkeypatch, mibank_html(rows), observer)
    assert without == with_obs and without[1] is ValueError


def test_duplicate_currency_rows_overwrite_as_before_and_are_conflict(real_mibank, monkeypatch, caplog):
    h = real_mibank
    rows = [mrow("USD", ["1", "1,390.00"]), mrow("JPY", ["1", "940.00"]),
            mrow("USD", ["1", "1,395.00"], via="flag"), mrow("EUR", ["1", "1,640.00"])]
    h.serve(bs, mibank_html(rows))
    bs.crawl_and_save_bs_bank_exchange_rates()
    assert h.writer.call_args.kwargs["current_rates"]["usd-krw"] == 1395.0, "기존 동작: 뒤 행이 덮어쓴다"
    mib = attempt(finished(h, bs), "mibank")
    usd = [o for o in mib["observations"] if o["pair"] == "usd-krw"]
    assert [(o["item_key"], o["rate"], o["code_basis"]) for o in usd] == [
        (0, 1390.0, "explicit_code_param"), (2, 1395.0, "flag_filename")]
    assert mib["collection"]["usd-krw"]["detail"] == "attribution_conflict"
    assert mib["collection"]["jpy-krw"]["reason"] == "v2_evidence_unconfirmed"


@pytest.mark.parametrize("empty_first", [True, False])
def test_duplicate_code_with_one_empty_row_is_still_conflict(real_mibank, empty_first):
    """값을 못 읽은 행도 그 통화의 원천이다 — 어느 행이 USD 인지 가를 수 없다(행 순서 무관)."""
    h = real_mibank
    usd_rows = [mrow("USD", ["1", "-"]), mrow("USD", ["1", "1,390.00"])]
    if not empty_first:
        usd_rows.reverse()
    rows = usd_rows + [mrow("JPY", ["1", "940.00"]), mrow("EUR", ["1", "1,640.00"])]
    h.serve(bs, mibank_html(rows))
    bs.crawl_and_save_bs_bank_exchange_rates()
    assert h.writer.call_args.kwargs["current_rates"] == MIBANK_RATES
    judged = attempt(finished(h, bs), "mibank")["collection"]["usd-krw"]
    assert judged["detail"] == "attribution_conflict"


def test_require_all_failure_keeps_prior_observations(real_mibank):
    h = real_mibank
    h.serve(bs, mibank_html([mrow("USD", ["1", "1,390.00"]), mrow("JPY", ["1", "940.00"])]))
    bs.crawl_and_save_bs_bank_exchange_rates()
    h.writer.assert_not_called()
    mib = attempt(finished(h, bs), "mibank")
    assert (mib["status"], mib["error_type"], mib["loop_completed"]) == ("failed", "RuntimeError", True)
    assert mib["collection"]["usd-krw"]["reason"] == "v2_evidence_unconfirmed"
    assert mib["collection"]["eur-krw"]["detail"] == "not_matched"


def test_nan_passes_range_check_but_is_rejected_in_report(real_mibank):
    h = real_mibank
    rows = [mrow("USD", ["1", "NaN"]), mrow("JPY", ["1", "940.00"]), mrow("EUR", ["1", "1,640.00"])]
    h.serve(bs, mibank_html(rows))
    bs.crawl_and_save_bs_bank_exchange_rates()
    rates = h.writer.call_args.kwargs["current_rates"]
    assert math.isnan(rates["usd-krw"]), "기존 동작: NaN 은 범위 검사를 통과해 writer 로 간다"
    event = finished(h, bs)
    mib = attempt(event, "mibank")
    assert mib["ops_range_check"]["termination"] == "returned"
    assert mib["ops_range_check"]["non_finite_input_pairs"] == ["usd-krw"]
    assert mib["deviation"]["pairs"]["usd-krw"]["pct"] == "nan", "직렬화가 종료 이벤트를 잃게 하지 않는다"
    judged = mib["collection"]["usd-krw"]
    assert (judged["status"], judged["reason"], judged["detail"]) == (
        "missing", "validation_rejected", "non_finite")


def test_range_check_failure_is_not_attributed_to_every_currency(real_mibank):
    h = real_mibank
    rows = [mrow("USD", ["1", "9,999.00"]), mrow("JPY", ["1", "940.00"]), mrow("EUR", ["1", "1,640.00"])]
    h.serve(bs, mibank_html(rows))
    bs.crawl_and_save_bs_bank_exchange_rates()
    h.writer.assert_not_called()
    mib = attempt(finished(h, bs), "mibank")
    assert (mib["status"], mib["error_type"]) == ("failed", "ValueError")
    assert mib["ops_range_check"] == {"termination": "raised", "error_type": "ValueError",
                                      "input_pairs": sorted(MIBANK_RATES), "non_finite_input_pairs": []}
    for pair in MIBANK_RATES:
        assert mib["collection"][pair]["reason"] == "v2_evidence_unconfirmed"
    assert "deviation" not in mib


def test_missing_prior_value_is_not_compared_rather_than_passed(real_mibank):
    h = real_mibank
    h.last.return_value = {"usd-krw": {"rate": None, "timestamp": None},
                           "jpy-krw": {"rate": 940.0, "timestamp": NOW - timedelta(minutes=5)},
                           "eur-krw": {"rate": 1640.0, "timestamp": None}}
    h.serve(bs, mibank_html(MIBANK_NORMAL))
    bs.crawl_and_save_bs_bank_exchange_rates()
    h.writer.assert_called_once()
    pairs = attempt(finished(h, bs), "mibank")["deviation"]["pairs"]
    assert pairs["usd-krw"] == {"compared": False, "reason": "prior_missing"}
    assert pairs["eur-krw"] == {"compared": False, "reason": "prior_missing"}
    assert pairs["jpy-krw"]["compared"] is True and pairs["jpy-krw"]["pct"] == 0.0


def test_citi_mibank_path_is_observed_through_the_same_routine(real_mibank):
    h = real_mibank
    h.serve(citi, mibank_html(MIBANK_NORMAL))
    citi.crawl_and_save_citi_bank_exchange_rates()
    h.writer.assert_called_once_with(db=h.db, current_rates=MIBANK_RATES, bank_name="citi")
    event = finished(h, citi)
    mib = attempt(event, "mibank")
    assert mib["loop_completed"] is True and len(mib["observations"]) == 3
    assert mib["adoption"]["decision"] == "submitted"
    assert event["summary"]["collection"]["usd-krw"]["path"] == "mibank"


@pytest.mark.parametrize("method", ["observed", "table_structure", "code_outside_required",
                                    "range_checked", "deviation_evaluated", "adoption"])
def test_mibank_observer_failure_changes_nothing(real_mibank, monkeypatch, method):
    h = real_mibank
    monkeypatch.setattr(bank_report.BankReport, method, Mock(side_effect=RuntimeError("probe")))
    h.serve(bs, mibank_html(MIBANK_NORMAL))
    assert bs.crawl_and_save_bs_bank_exchange_rates() is None
    h.writer.assert_called_once_with(db=h.db, current_rates=MIBANK_RATES, bank_name="bs")
    event = finished(h, bs)
    assert method in event["telemetry_errors"]
    judged = attempt(event, "mibank")["collection"]["usd-krw"]
    assert (judged["status"], judged["reason"]) == ("unknown", "evidence_incomplete")


def test_mibank_without_observer_returns_same_rates_and_logs_nothing(harness, monkeypatch):
    monkeypatch.setattr(utils.requests, "get", Mock(return_value=page(mibank_html(MIBANK_NORMAL))))
    assert utils.crawl_mibank_rates("u", "bs") == MIBANK_RATES
    assert harness.events(bs) == []


def test_structure_header_facts_failure_keeps_column_basis(real_mibank, monkeypatch):
    h = real_mibank
    real_snippet = bank_report._snippet

    def failing_on_header(text):
        if text is not None and "기준환율" in str(text) and "통화" in str(text):
            raise RuntimeError("header")
        return real_snippet(text)

    monkeypatch.setattr(bank_report, "_snippet", failing_on_header)
    h.serve(bs, mibank_html(MIBANK_NORMAL))
    bs.crawl_and_save_bs_bank_exchange_rates()
    structure = attempt(finished(h, bs), "mibank")["structure"]
    assert (structure["column_index"], structure["column_basis"]) == (2, "label_found")
    assert structure["header_facts_error"] == "RuntimeError"


def test_old_helpers_return_first_element_of_basis_helpers():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(mibank_html(MIBANK_NORMAL + [mrow("GBP", ["1", "-"])]), "html.parser")
    tbody = soup.select_one("div.box_contents1 table tbody")
    index = utils._get_mibank_base_rate_column_index(tbody)
    assert index == utils._mibank_base_rate_column_with_basis(tbody)[0] == 2
    for row in tbody.find_all("tr"):
        assert utils._extract_mibank_currency_code(row) == utils._mibank_currency_code_with_basis(row)[0]
        for idx in (None, index, 9):
            assert utils._extract_mibank_rate_text(row, idx) == \
                utils._mibank_rate_text_with_basis(row, idx)[0]


def test_outside_required_codes_are_bounded_but_counted(monkeypatch, caplog):
    monkeypatch.setattr(bank_report, "MAX_OUTSIDE_REQUIRED_CODES", 2)
    extra = [mrow(code, ["1", "10.00"], via=via)
             for code, via in (("CNY", "href"), ("GBP", "flag"), ("AUD", "href"))]
    html = mibank_html(extra + [mrow("USD", ["1", "1,390.00"]), mrow("JPY", ["1", "940.00"]),
                                mrow("EUR", ["1", "1,640.00"])])
    report, observer = _observer(monkeypatch, caplog)
    without, with_obs = _mibank_both_ways(monkeypatch, html, observer)
    assert without == with_obs == ("returned", MIBANK_RATES)
    assert report.attempts[bank_report.MIBANK]["outside_required_codes"] == {
        "codes": ["CNY", "GBP"], "basis_counts": {"explicit_code_param": 2, "flag_filename": 1},
        "truncated": True}


def _finished_event_bytes(h, module):
    [event] = h.events(module, "bank_round_finished")
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode())


@pytest.mark.parametrize("empty_rows", [20, 200])
def test_many_empty_rows_for_one_code_are_bounded_and_keep_the_conflict(real_mibank, empty_rows):
    h = real_mibank
    rows = [mrow("USD", ["1", "-"])] * empty_rows + [mrow("USD", ["1", "1,390.00"]),
                                                    mrow("JPY", ["1", "940.00"]),
                                                    mrow("EUR", ["1", "1,640.00"])]
    h.serve(bs, mibank_html(rows))
    bs.crawl_and_save_bs_bank_exchange_rates()
    h.writer.assert_called_once_with(db=h.db, current_rates=MIBANK_RATES, bank_name="bs")
    event = finished(h, bs)
    mib = attempt(event, "mibank")
    assert event["truncated"] is True and mib["misses_truncated"] is True
    usd_misses = [m for m in mib["misses"] if m["pair"] == "usd-krw"]
    assert len(usd_misses) == bank_report.MAX_MISSES_PER_PAIR
    assert mib["dropped"]["misses"]["usd-krw"] == empty_rows - bank_report.MAX_MISSES_PER_PAIR
    # 남긴 miss 들만으로 이미 확인된 충돌은 잘림보다 먼저 확정된다.
    assert mib["collection"]["usd-krw"]["detail"] == "attribution_conflict"
    assert mib["collection"]["jpy-krw"]["reason"] == "evidence_incomplete"
    assert _finished_event_bytes(h, bs) < 12_000


def test_long_outside_code_is_clipped_in_the_record_only(monkeypatch, caplog):
    long_code = "A" * 10_000
    html = mibank_html([mrow(long_code, ["1", "10.00"])] + MIBANK_NORMAL)
    report, observer = _observer(monkeypatch, caplog)
    without, with_obs = _mibank_both_ways(monkeypatch, html, observer)
    assert without == with_obs == ("returned", MIBANK_RATES)
    outside = report.attempts[bank_report.MIBANK]["outside_required_codes"]
    assert outside["codes"][0] == "A" * bank_report.MAX_CODE_CHARS
    assert outside["code_text_clipped"] is True


def test_per_row_telemetry_failures_do_not_grow_the_error_list(real_mibank, monkeypatch):
    h = real_mibank
    monkeypatch.setattr(bank_report.BankReport, "missed", Mock(side_effect=RuntimeError("probe")))
    rows = [mrow("USD", ["1", "-"])] * 50 + MIBANK_NORMAL
    h.serve(bs, mibank_html(rows))
    bs.crawl_and_save_bs_bank_exchange_rates()
    event = finished(h, bs)
    assert event["telemetry_errors"] == ["missed"]
    assert event["telemetry_error_counts"] == {"missed": 50}


def test_citi_usd_observed_and_another_item_usd_selector_miss_is_conflict(harness):
    """P2 회귀 잠금: 한 항목은 USD 값, 다른 항목은 USD 문구가 있으나 값 요소가 없다."""
    html = ('<div id="content"><ul>'
            '<li><div><div>미국(USD)</div><div><span>1,393.50</span></div></div></li>'
            '<li><div><div>미국(USD) 공지</div><div>값 없음</div></div></li>'
            '<li><div><div>일본(JPY)</div><div><span>942.64</span></div></div></li>'
            '</ul></div>')
    harness.http.return_value = page(html)
    citi.crawl_and_save_citi_bank_exchange_rates()
    assert harness.writer.call_args.kwargs["current_rates"] == {"usd-krw": 1393.5, "jpy-krw": 942.64}
    primary = attempt(finished(harness, citi), "official_primary")
    [miss] = [m for m in primary["misses"] if m["pair"] == "usd-krw"]
    assert (miss["item_key"], miss["reason"]) == ("2nd", "selector_miss")
    assert primary["collection"]["usd-krw"]["detail"] == "attribution_conflict"
    assert primary["collection"]["jpy-krw"]["reason"] == "v2_evidence_unconfirmed"
