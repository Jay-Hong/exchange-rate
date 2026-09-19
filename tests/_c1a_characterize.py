"""C1a 특성 기록기 — 추출 경로의 관찰 가능한 동작을 순서대로 기록한다(시험 모듈이 아니다).

기록: 로그(로거·수준·문구·extra·예외 정보 유무), 보고 관측자 호출(메서드·인자), 예외(종류·문구), 요청 인자·횟수, writer 입력,
DB 직전값 조회 인자, 반환값. 표준 로그 필드(함수 이름·줄 번호)는 기록하지 않는다 — 리팩터로 호출 위치가 바뀌는 것은 동작이 아니다.

⛔ 원래부터 있던 진입점만 부른다(공식 루틴 3개, `crawl_mibank_rates`, 9개 은행의 `_crawl_mibank_*`). 그래야 리팩터 전 코드에서
같은 기록기로 기준 파일(`tests/fixtures/c1a_pre_refactor_behavior.json`)을 만들 수 있다. 생성: 리포 루트에서
`python3 -m tests._c1a_characterize <출력 json>`.
"""
import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from unittest.mock import Mock

import requests
from bs4.element import Tag

from app import crud, models
from app.crawlers import bs, citi, hana, ibk, kb, nh, sc, shinhan, utils, woori
from tests.test_bank_report import (
    BS_NORMAL, CITI_NORMAL, MIBANK_NORMAL, bs_html, citi_first_html, citi_second_html, mibank_html, mrow, page)

BANKS = (bs, citi, hana, ibk, kb, nh, sc, shinhan, woori)
PRODUCER_FILES = ("app/crawlers/utils.py", "app/crawlers/bs.py", "app/crawlers/citi.py")
NOW = datetime(2026, 9, 18, 3, 0)
PRIOR = {"usd-krw": 1390.0, "jpy-krw": 940.0, "eur-krw": 1640.0}
_STD = set(vars(logging.LogRecord("x", 0, "", 0, "", (), None))) | {"message", "asctime", "taskName"}


def norm(value):
    if isinstance(value, Tag):
        return {"tag": str(value)}
    if isinstance(value, BaseException):
        return {"exception": type(value).__name__, "message": str(value)}
    if isinstance(value, dict):
        return {str(k): norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [norm(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class _Handler(logging.Handler):
    def __init__(self, events):
        super().__init__(logging.DEBUG)
        self.events = events

    def emit(self, record):
        extra = {k: norm(v) for k, v in record.__dict__.items() if k not in _STD}
        self.events.append(["log", record.name, record.levelname, record.getMessage(), extra, bool(record.exc_info)])


class _Recorder:
    def __init__(self, events):
        self._events = events

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self._events.append(["obs", name, norm(list(args)), norm(kwargs)])
        return call


def _run(fn, response):
    """response: HTML 문자열 또는 요청이 던질 예외. 반환: 한 경우의 기록."""
    events = []
    handler = _Handler(events)
    loggers = [module.logger for module in (*BANKS, utils)]
    saved = [(lg, lg.level, lg.propagate) for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
    target = response if isinstance(response, BaseException) else page(response)
    fake_get = Mock(side_effect=[target])
    writer = Mock(return_value=3)
    last = Mock(return_value={pair: {"rate": rate, "timestamp": NOW - timedelta(minutes=5)}
                              for pair, rate in PRIOR.items()})
    originals = (requests.get, crud.insert_bank_rates_into_db, crud.get_last_bank_rates_with_ts, models.get_utc_now)
    requests.get = fake_get
    crud.insert_bank_rates_into_db, crud.get_last_bank_rates_with_ts = writer, last
    models.get_utc_now = Mock(return_value=NOW)
    try:
        outcome = ["return", norm(fn(events))]
    except Exception as exc:  # 기록기는 예외 종류·문구까지 남긴다
        outcome = ["raise", type(exc).__name__, str(exc)]
    finally:
        requests.get, crud.insert_bank_rates_into_db, crud.get_last_bank_rates_with_ts, models.get_utc_now = originals
        for lg, level, propagate in saved:
            lg.removeHandler(handler)
            lg.setLevel(level)
            lg.propagate = propagate
    return {
        "events": events, "outcome": outcome,
        "requests": [[list(c.args), norm(c.kwargs)] for c in fake_get.call_args_list],
        "writer": [norm({k: v for k, v in c.kwargs.items() if k != "db"} | {"observer": c.kwargs.get("observer") is not None})
                   for c in writer.call_args_list],
        "last_rates_queries": [norm(list(c.args[1:])) for c in last.call_args_list],
    }


def _obs(events, with_observer):
    return _Recorder(events) if with_observer else None


def cases():
    out = {}
    for with_obs in (True, False):
        tag = "obs" if with_obs else "noobs"
        official = (("normal", BS_NORMAL), ("miss_row", BS_NORMAL[:2]),
                    ("parse_error", [("미국 USD", "N/A")] + BS_NORMAL[1:]), ("empty", []))
        for name, rows in official:
            out[f"bs_official_{name}_{tag}"] = (lambda o: lambda ev: bs.crawl_and_save_routine(
                "https://example.invalid/b", bs.BS_BANK_SELECTORS, Mock(), observer=_obs(ev, o)))(with_obs), bs_html(rows)
            out[f"citi_second_{name}_{tag}"] = (lambda o: lambda ev: citi.crawl_and_save_routine(
                "https://example.invalid/c2", citi.SECOND_CITI_BANK_SELECTORS, Mock(), observer=_obs(ev, o)))(with_obs), \
                citi_second_html(rows)
        out[f"bs_official_http_error_{tag}"] = (lambda o: lambda ev: bs.crawl_and_save_routine(
            "https://example.invalid/b", bs.BS_BANK_SELECTORS, Mock(), observer=_obs(ev, o)))(with_obs), \
            requests.ConnectionError("down")
        first = (("normal", CITI_NORMAL), ("two_codes_one_item", [("USD JPY", "1,393.50")] + CITI_NORMAL[1:]),
                 ("parse_error", [("미국(USD)", "abc")] + CITI_NORMAL[1:]), ("short", CITI_NORMAL[:2]),
                 ("overwrite", [("미국(USD)", "1,393.50"), ("미국(USD)", "1,400.00")]))
        for name, items in first:
            out[f"citi_first_{name}_{tag}"] = (lambda o: lambda ev: citi.crawl_and_save_citi_first_routine(
                "https://example.invalid/c1", citi.CITI_BANK_SELECTORS, Mock(), observer=_obs(ev, o)))(with_obs), \
                citi_first_html(items)
        out[f"citi_first_no_span_{tag}"] = (lambda o: lambda ev: citi.crawl_and_save_citi_first_routine(
            "https://example.invalid/c1", citi.CITI_BANK_SELECTORS, Mock(), observer=_obs(ev, o)))(with_obs), \
            '<div id="content"><ul><li><div><div>미국(USD)</div><div></div></div></li></ul></div>'
        mib = {
            "normal": mibank_html(MIBANK_NORMAL),
            "no_header": mibank_html(MIBANK_NORMAL, header=None),
            "short_row": mibank_html([mrow("USD", ["1,380.00"])] + MIBANK_NORMAL[1:]),
            "dash_value": mibank_html([mrow("USD", ["1,380.00", "-"])] + MIBANK_NORMAL[1:]),
            "parse_error": mibank_html([mrow("USD", ["1,380.00", "x.y"])] + MIBANK_NORMAL[1:]),
            "missing_required": mibank_html(MIBANK_NORMAL[:2]),
            "duplicate_code": mibank_html(MIBANK_NORMAL + [mrow("USD", ["1,381.00", "1,391.00"])]),
            "no_table": "<div>nothing</div>",
            "counter_span": mibank_html([mrow("USD", ["1,380.00", '<span class="counter">1,390.50</span>'])]
                                        + MIBANK_NORMAL[1:]),
            "out_of_range": mibank_html([mrow("USD", ["1,380.00", "99,999.00"])] + MIBANK_NORMAL[1:]),
        }
        for name, html in mib.items():
            for require_all in (True, False):
                out[f"mibank_{name}_req{int(require_all)}_{tag}"] = (lambda o, r: lambda ev: utils.crawl_mibank_rates(
                    "https://example.invalid/m", "bs", require_all=r, observer=_obs(ev, o)))(with_obs, require_all), html
        for module in (bs, citi):
            for name in ("normal", "parse_error", "missing_required", "no_table", "out_of_range"):
                helper = getattr(module, f"_crawl_mibank_{module.BANK_NAME}")
                out[f"bank_{module.BANK_NAME}_mibank_{name}_{tag}"] = (
                    lambda h, o: lambda ev: h(Mock(), observer=_obs(ev, o)))(helper, with_obs), mib[name]
    for module in BANKS:
        if module in (bs, citi):
            continue
        helper = getattr(module, f"_crawl_mibank_{module.BANK_NAME}")
        for name in ("normal", "parse_error", "missing_required", "no_table", "out_of_range"):
            out[f"bank_{module.BANK_NAME}_mibank_{name}_noobs"] = (lambda h: lambda ev: h(Mock()))(helper), mib[name]
    return out


def characterize():
    return {label: _run(fn, response) for label, (fn, response) in sorted(cases().items())}


def _producer():
    blobs = {}
    for path in PRODUCER_FILES:
        blobs[path] = subprocess.check_output(["git", "hash-object", path], text=True).strip()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    return {"head": head, "blobs": blobs}


if __name__ == "__main__":
    result = {"producer": _producer(), "cases": characterize()}
    text = json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"cases={len(result['cases'])} sha256={hashlib.sha256(text.encode()).hexdigest()}")
