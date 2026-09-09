"""IBK 전체 흐름 — **실제 생성기 → 실제 프레임 → 부모 계측 → 경보 큐·발송**.

여기서 진짜인 것: 공식 파서, SQLite·CRUD 저장, strict 캡처와 그 드라이버 어댑터, 결과
프로토콜의 인코딩·복호·검증, runner 의 방출 경로, 부모의 판정·계측, 경보 전달 모듈의 큐와
소비자 스레드.

대체하는 것은 **경계 셋과 기동 한 겹**이다: `requests.post` 응답, WebDriver 객체,
Telegram 발송, 그리고 자식 프로세스 기동. HTTP 는 응답 객체를 주어 **실제 파서가 돌고**,
Selenium 은 드라이버를 주어 **실제 어댑터 조작과 strict 판정이 돈다** — 둘 다 함수를 통째로
가로채지 않는다. ⛔ 통째로 가로채면 "파서·판정이 진짜" 라는 말이 거짓이 된다(초판이 그랬다).

⛔ Selenium 경로는 **서비스 날짜가 이미 맞는** 화면을 준다. 제출·확인 하위 경로는 3.5초
   연속 제출 가드를 실제로 기다리므로 여기서 돌리지 않는다 — 그 경로는
   `tests/test_ibk_selenium_strict.py` 와 `tests/test_ibk_selenium_adapter.py` 가 덮는다.
프레임 바이트는 진짜 `os.pipe()` 로 실제 runner 가 쓴 것을 읽어 부모에게 넘긴다 — 즉
`asyncio.create_subprocess_exec` 한 겹만 빠진다. 그 겹은
`tests/test_ibk_runner_wiring.py::test_real_bootstrap_runner_to_parent_and_queue` 가
실제 subprocess 로 덮는다(대신 그쪽은 합성 결과를 쓴다).

⛔ 금지 호출은 **예외를 던지는 가드로 확인하지 않는다.** 안전망의 바깥 `except Exception`
   이 그 `AssertionError` 까지 삼켜 `driver_failed:AssertionError` 라는 관측 결과로 바꾸므로,
   가드가 발화해도 시험은 통과한다. 그래서 **호출 횟수를 직접 센다.**
"""

import datetime
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import atomic_write_runtime as awr
from app import crud, models
from app.crawlers import ibk, runner
from app.ibk_notice_delivery import IbkNoticeDelivery
from app.ibk_parent_runner import IbkParentRunner
from app.ibk_result_protocol import (
    IbkExecutionFailure, IbkReason, IbkSource, IbkStatus,
)
from app.ibk_subprocess_capture import IbkProcessCapture

KST = ibk.KST
REFERENCE = KST.localize(datetime.datetime(2026, 8, 28, 6, 5))
SERVICE_DATE = datetime.date(2026, 8, 27)
COMPLETED_AT = "05:59:55"
COMPLETION = ibk._ibk_completion_kst(SERVICE_DATE, COMPLETED_AT)
RATES = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
NOW = REFERENCE.astimezone(datetime.timezone.utc)
OTHER = {"usd-krw": 1383.5, "jpy-krw": 868.5, "eur-krw": 1611.5}


def _official_html(*, selected_date="2026.08.27", completed_at=COMPLETED_AT, rates=None):
    """실제 파서를 통과시키는 최소 공식 응답. 파서를 우회하지 않기 위해 HTML 로 준다."""
    values = RATES if rates is None else rates
    body = ("<thead><tr><th>통화</th><th>통화명</th><th>매매기준율</th><th>기타</th></tr></thead>"
            "<tbody>" + "".join(
                f"<tr><th>{code}</th><th>{code} name</th><td>{rate}</td><td>0</td></tr>"
                for code, rate in (("USD", values["usd-krw"]), ("JPY", values["jpy-krw"]),
                                   ("EUR", values["eur-krw"]))) + "</tbody>")
    return (f'<html><body><input id="inDate" value="{selected_date}">'
            f"<p class='standard'>고시완료 시각 : {completed_at}</p>"
            f'<table><caption>일반고시환율 표</caption>{body}</table></body></html>')


def _rejected_html(selected_date):
    """strict 가 **거부**하는 화면. 날짜 readback 은 통과하지만 필수 통화가 빠져 파서가
    `ValueError` 를 내고, 캡처러가 그것을 REJECTED 로 접는다."""
    body = ("<thead><tr><th>통화</th><th>통화명</th><th>매매기준율</th><th>기타</th></tr></thead>"
            "<tbody>" + "".join(
                f"<tr><th>{code}</th><th>{code} name</th><td>{rate}</td><td>0</td></tr>"
                for code, rate in (("USD", RATES["usd-krw"]), ("JPY", RATES["jpy-krw"])))
            + "</tbody>")            # EUR 없음
    return (f'<html><body><input id="inDate" value="{selected_date}">'
            f"<p class='standard'>고시완료 시각 : {COMPLETED_AT}</p>"
            f'<table><caption>일반고시환율 표</caption>{body}</table></body></html>')


def _no_session_html(selected_date):
    """공식 무고시 화면. ⛔ 파서는 날짜 readback 을 **먼저** 보므로 `#inDate` 가 없으면
    무고시가 아니라 계약 오류가 되어 안전망이 열린다(실측)."""
    return (f'<html><body><input id="inDate" value="{selected_date}">'
            f'<p>ECBKFEX01589</p></body></html>')


def _http_response(html):
    response = MagicMock()
    response.text = html
    response.raise_for_status.return_value = None
    return response


class _FakeDriver:
    """실제 어댑터 조작(`app/ibk_selenium_adapter.py`)이 그대로 도는 최소 드라이버.

    ⛔ 캡처러나 안전망을 통째로 가로채지 않는다 — 그러면 strict 판정이 돌지 않는다.
       여기서 대체하는 것은 **WebDriver 객체** 하나다.
    """

    def __init__(self, *, served, html):
        self.served, self.page_source = served, html
        self.gets = 0

    # 어댑터가 쓰는 표면 ---------------------------------------------------
    def get(self, url):
        self.gets += 1

    def find_element(self, by, selector):
        return MagicMock(name=f"element:{selector}")

    def execute_script(self, script, *args):
        from app.ibk_selenium_adapter import READY_STATE_SCRIPT, SERVED_DATE_SCRIPT
        if script == SERVED_DATE_SCRIPT:
            return self.served
        if script == READY_STATE_SCRIPT:
            return "complete"
        raise AssertionError(f"예상치 못한 스크립트: {script}")

    @property
    def switch_to(self):
        from selenium.common.exceptions import NoAlertPresentException
        raise NoAlertPresentException()

    # 드라이버 제한은 관측을 막지 않는다(실패해도 무시되는 경로).
    def set_page_load_timeout(self, value):
        pass

    def set_script_timeout(self, value):
        pass



class _Frame:
    """자식이 실제로 쓴 프레임 바이트와 종료 코드."""

    def __init__(self, code, payload):
        self.code, self.payload = code, payload


class EndToEndTest(unittest.TestCase):
    """⛔ 이 클래스가 보는 것은 '조각이 각각 맞다' 가 아니라 **이어 붙였을 때 뜻이 유지되는가**다."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = handle.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        self.engine = create_engine(f"sqlite:///{self.path}")
        models.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.Session = sessionmaker(bind=self.engine)

        # 생성기가 여는 세션을 이 임시 DB 로 돌린다.
        session_patch = patch.object(ibk, "SessionLocal", self.Session)
        session_patch.start()
        self.addCleanup(session_patch.stop)

        for name, value in (("_write_changed_bank_rates_to_redis", []),
                            ("process_rate_alerts", 0),
                            ("_emit_fx_alert_canary", False),
                            ("_emit_fx_alert_shadow", None),
                            ("_emit_topic_triggers", None)):
            patcher = patch.object(crud, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        from app import atomic_write_refresh
        refresh_patch = patch.object(atomic_write_refresh, "refresh_write_mode_cache", MagicMock())
        refresh_patch.start()
        self.addCleanup(refresh_patch.stop)

        # 금지 호출 계수기. 예외 가드가 아니라 **횟수**로 확인한다.
        # ⛔ 계수와 차단을 분리한다. 하나로 두면 드라이버를 **일부러 주입하는** 사례에서도
        #    가드가 터지고, 반대로 가드를 빼면 흐름이 mock 드라이버로 조용히 계속된다.
        self.driver_opens = MagicMock(name="selenium_driver_context")

        def refuse_driver(*args, **kwargs):
            self.driver_opens(*args, **kwargs)
            raise AssertionError("드라이버를 주입하지 않은 사례에서 열면 안 된다")

        driver_patch = patch.object(ibk, "selenium_driver_context", refuse_driver)
        driver_patch.start()
        self.addCleanup(driver_patch.stop)
        self.mibank = MagicMock(side_effect=AssertionError("MIBANK 를 부르면 안 된다"))
        mibank_patch = patch.object(ibk, "_crawl_mibank_ibk", self.mibank)
        mibank_patch.start()
        self.addCleanup(mibank_patch.stop)

    # ── 흐름 조립 ─────────────────────────────────────────────

    def _seed(self, rates, saved_offset):
        """후보 완료시각 기준 오프셋으로 기존 DB 행을 심는다."""
        moment = (COMPLETION + datetime.timedelta(seconds=saved_offset)
                  ).astimezone(datetime.timezone.utc).replace(tzinfo=None)
        with self.Session() as seed:
            for pair, rate in rates.items():
                seed.add(models.BankExchangeRate(bank=ibk.BANK_NAME, currency=pair,
                                                 rate=rate, timestamp=moment))
            seed.commit()

    def _all_rows(self):
        """행 전체를 정렬해 돌려준다. ⛔ 통화별 **최신 rate** 만 비교하면 같은 값의 중복
        INSERT 나 timestamp 만 바뀌는 UPDATE 가 그대로 통과한다 — "DB 불변" 을 주장하려면
        행 자체를 봐야 한다."""
        with self.Session() as fresh:
            return sorted(
                (row.id, row.bank, row.currency, row.rate, row.timestamp)
                for row in fresh.query(models.BankExchangeRate).all())

    def _rows(self):
        with self.Session() as fresh:
            return fresh.query(models.BankExchangeRate).count()

    def _saved_rates(self):
        with self.Session() as fresh:
            return {row.currency: row.rate
                    for row in fresh.query(models.BankExchangeRate).all()}

    def _child(self, args, *, http, driver=None):
        """**실제 runner** 를 in-process 로 돌려 진짜 프레임 바이트를 얻는다.

        인자는 **부모가 자식에게 넘긴 것 그대로**다 — 시험이 별도 context 를 만들면 run_id 가
        달라 부모가 자기 프레임을 거부한다(실측: OBSERVED 0회).
        `os.pipe()` 로 runner 가 쓴 바이트를 읽는다. 프레임을 시험이 만들지 않는다.

        `http` 는 `requests.post` 를 대체한다 — 파서는 그 응답을 실제로 읽는다.
        `driver` 를 주면 안전망이 그것을 열어 **실제 어댑터·strict 판정**이 돈다.
        """
        import contextlib

        read_fd, write_fd = os.pipe()
        stack = [patch.object(ibk.requests, "post", **http),
                 patch.object(ibk, "_is_preopen_pending_window", return_value=False)]
        if driver is not None:
            @contextlib.contextmanager
            def opener():
                self.driver_opens()          # 계수는 그대로 유지한다
                yield driver

            stack.append(patch.object(ibk, "selenium_driver_context", opener))
        for item in stack:
            item.start()
        try:
            code = runner.main(
                argv=args, result_fd=write_fd,
                # 관측 시각을 기준 시각에 맞춘다 — 부모의 `validate_result` 가 둘의 격차를 본다.
                ibk_result_crawler=lambda ctx: ibk.run_ibk_dated_result(ctx, now=NOW))
        finally:
            for item in reversed(stack):
                item.stop()
            os.close(write_fd)
        try:
            payload = b""
            while True:
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                payload += chunk
        finally:
            os.close(read_fd)
        return _Frame(code, payload)

    def _run_flow(self, *, http, sink, driver=None, timeout=45,
                  monotonic_offset=0.0, timed_out=False, seed_official_clock=None):
        """부모 → 자식 → 프레임 → 부모 판정 → 경보까지 한 번 돌린다.

        대체하는 것은 **자식 기동 한 겹**뿐이다. 부모가 만든 argv 를 그대로 받아 실제 runner
        를 돌리고, 그 바이트를 부모에게 돌려준다.
        """
        import asyncio
        import time as _time

        frames = []

        async def fake_capture(argv, **kwargs):
            frame = self._child(tuple(argv[3:]), http=http, driver=driver)
            frames.append(frame)
            # ⛔ 시간 초과라고 stdout 을 비우지 않는다. 자식이 프레임을 다 쓰고도 부모가
            #    기다리다 끊는 경우가 이 축의 실제 관심사다 — 비우고 returncode 까지 1 로
            #    두면 `timed_out` 없이도 같은 결과가 나와 신호가 격리되지 않는다(실측:
            #    `timed_out` 분기를 제거한 변이가 생존했다).
            return IbkProcessCapture(frame.code, frame.payload, b"",
                                     len(frame.payload), 0, timed_out, False, None)

        parent = IbkParentRunner(
            capture=fake_capture, event_sink=sink,
            clock=lambda: REFERENCE,
            monotonic=lambda: _time.monotonic() + monotonic_offset)
        if seed_official_clock is not None:
            # 이미 최신 공식 시각이 있는 부모 — 이번 회차가 그것을 되돌리는지 본다.
            parent.last_current_official_at = seed_official_clock
        decision = asyncio.run(parent.execute(timeout=timeout))
        return parent, decision, frames[0]

    def _db_pairs(self):
        """DB 에 **값이 있는** 통화만. ⛔ `if info` 로 거르면 안 된다 — crud 는 행이 없어도
        `{"rate": None, "timestamp": None}` 을 돌려주고 그 사전은 참이라, 빈 DB 에서도 세
        통화가 다 있는 것으로 잡힌다(실측). 그러면 아래 공용 단언이 항상 '완전' 을 기대해
        빈·부분 DB 를 검사하지 못한다."""
        with self.Session() as fresh:
            rows = crud.get_last_bank_rates_with_ts(
                fresh, ibk.BANK_NAME, list(ibk.MIBANK_REQUIRED_PAIRS))
        return {pair: info["rate"] for pair, info in rows.items()
                if info and info.get("rate") is not None}

    def _assert_agrees_with_db(self, result):
        """부모가 받은 DB 계측이 실제 DB 와 같은 말을 하는가."""
        present = self._db_pairs()
        self.assertEqual(set(result.preserved_pairs or ()), set(present),
                         f"DB 에 있는 통화와 보고가 다르다: {present}")
        self.assertEqual(set(result.missing_pairs or ()),
                         set(ibk.MIBANK_REQUIRED_PAIRS) - set(present))
        self.assertEqual(result.db_snapshot_complete,
                         set(present) == set(ibk.MIBANK_REQUIRED_PAIRS))

    def _delivery(self, *, send):
        delivery = IbkNoticeDelivery(send=send, retry_seconds=0.0, poll_seconds=0.01)
        delivery.start()
        self.addCleanup(lambda: delivery.close(timeout=2.0))
        return delivery

    def _await(self, predicate, *, timeout=3.0):
        """소비자 스레드가 처리할 때까지 기다린다. 고정 sleep 을 쓰지 않는다."""
        import time as _time
        limit = _time.monotonic() + timeout
        while _time.monotonic() < limit:
            if predicate():
                return True
            _time.sleep(0.01)
        return False


class TheDbOutcomeAndTheParentStateAgree(EndToEndTest):
    """축 1 — 저장한 것과 부모가 기록한 상태가 **같은 사실**을 말하는가.

    ⛔ "성공하면 저장된다" 만 보면 좁다. 부모가 받은 `changed_count`·`preserved_pairs`·
       `missing_pairs`·`db_snapshot_complete` 를 DB 재조회와 함께 대조한다 — 그 넷 중 하나가
       DB 와 어긋나면 운영자는 있지도 않은 상태를 본다.
    """

    def test_an_official_post_success_is_saved_and_counted_as_observed(self):
        sink = []
        parent, decision, frame = self._run_flow(
            http={"return_value": _http_response(_official_html())},
            sink=lambda n: sink.append(n) or True)

        self.assertEqual(frame.code, 0)
        self.assertEqual(self._saved_rates(), RATES, "실제 파서가 읽은 값이 DB 에 들어간다")
        self.assertIs(decision.result.status, IbkStatus.OBSERVED)
        self.assertIs(decision.result.source, IbkSource.OFFICIAL_POST)
        self.assertEqual(decision.result.changed_count, 3, "세 통화가 새로 저장됐다")
        self._assert_agrees_with_db(decision.result)
        self.assertEqual(parent.counts["OBSERVED"], 1)
        self.assertIsNotNone(parent.last_current_official_at)
        self.assertEqual(sink, [], "정상 회차는 경보를 만들지 않는다")

    def test_the_same_value_observed_again_changes_nothing_but_is_still_observed(self):
        """⛔ '변경 0' 을 실패로 읽으면 안 된다. 관측은 성공했고 저장할 것이 없었을 뿐이다."""
        self._seed(RATES, -3600)
        parent, decision, _ = self._run_flow(
            http={"return_value": _http_response(_official_html())}, sink=lambda n: True)

        self.assertIs(decision.result.status, IbkStatus.OBSERVED)
        self.assertEqual(decision.result.changed_count, 0)
        self.assertEqual(self._rows(), 3, "새 행이 생기지 않았다")
        self._assert_agrees_with_db(decision.result)
        self.assertEqual(parent.counts["OBSERVED"], 1)

    def test_a_blocked_write_with_a_complete_db_is_degraded_not_observed(self):
        """쓰기가 막히면 관측했어도 승격하지 않는다 — DB 는 기존 값을 그대로 들고 있다."""
        self._seed(OTHER, -3600)
        awr._reset_for_test()
        parent, decision, _ = self._run_flow(
            http={"return_value": _http_response(_official_html())}, sink=lambda n: True)

        self.assertIs(decision.result.status, IbkStatus.DEGRADED)
        self.assertIs(decision.result.reason, IbkReason.WRITE_POLICY_BLOCKED)
        self.assertEqual(self._db_pairs(), OTHER, "한 줄도 바뀌지 않았다")
        self._assert_agrees_with_db(decision.result)
        self.assertEqual(parent.counts["DEGRADED"], 1)
        self.assertIsNone(parent.last_current_official_at,
                          "저장하지 못한 관측은 최신 공식 시각을 앞당기지 않는다")

    def test_a_partial_regression_reports_what_it_kept_and_what_it_wrote(self):
        """유지한 값과 새로 저장한 값이 DB·부모 양쪽에서 같은 말을 해야 한다."""
        tolerance = ibk.IBK_DB_SAVE_LAG_TOLERANCE_SECONDS
        self._seed({"usd-krw": OTHER["usd-krw"], "jpy-krw": OTHER["jpy-krw"]}, tolerance + 1)
        self._seed({"eur-krw": OTHER["eur-krw"]}, -1)
        parent, decision, _ = self._run_flow(
            http={"return_value": _http_response(_official_html())}, sink=lambda n: True)

        self.assertIs(decision.result.status, IbkStatus.DEGRADED)
        self.assertIs(decision.result.reason, IbkReason.REGRESSION_GUARD)
        self.assertEqual(decision.result.changed_count, 1, "eur 만 저장됐다")
        present = self._db_pairs()
        self.assertEqual(present["usd-krw"], OTHER["usd-krw"], "회귀로 제외된 값은 유지된다")
        self.assertEqual(present["eur-krw"], RATES["eur-krw"], "보충된 값은 새 관측이다")
        self._assert_agrees_with_db(decision.result)
        self.assertEqual(parent.counts["DEGRADED"], 1)

    def test_an_empty_db_is_reported_as_empty_not_complete(self):
        """⛔ 공용 단언이 공허하지 않으려면 **완전하지 않은 DB** 에서도 돌아야 한다.
        무고시 + 빈 DB — 보고는 '아무것도 없다' 여야 한다."""
        parent, decision, _ = self._run_flow(
            http={"side_effect": lambda url, **kw:
                  _http_response(_no_session_html(kw["data"]["inDate"]))},
            sink=lambda n: True)

        self.assertEqual(self._db_pairs(), {}, "양성 대조 — DB 가 비어 있다")
        self.assertEqual(decision.result.preserved_pairs, ())
        self.assertEqual(set(decision.result.missing_pairs or ()),
                         set(ibk.MIBANK_REQUIRED_PAIRS))
        self.assertFalse(decision.result.db_snapshot_complete)
        self._assert_agrees_with_db(decision.result)
        self.assertIs(decision.result.status, IbkStatus.FAILED,
                      "보존할 것이 없으면 보존이 아니다")

    def test_a_partial_db_is_reported_as_partial(self):
        """일부 통화만 있는 DB — 있는 것과 없는 것이 각각 제자리에 보고돼야 한다."""
        self._seed({"usd-krw": OTHER["usd-krw"]}, -3600)
        parent, decision, _ = self._run_flow(
            http={"side_effect": lambda url, **kw:
                  _http_response(_no_session_html(kw["data"]["inDate"]))},
            sink=lambda n: True)

        self.assertEqual(set(self._db_pairs()), {"usd-krw"}, "양성 대조 — 한 통화만 있다")
        self.assertEqual(set(decision.result.preserved_pairs or ()), {"usd-krw"})
        self.assertEqual(set(decision.result.missing_pairs or ()),
                         {"jpy-krw", "eur-krw"})
        self.assertFalse(decision.result.db_snapshot_complete)
        self._assert_agrees_with_db(decision.result)

    def test_a_verified_selenium_success_takes_the_same_save_path(self):
        """⛔ 저장 규칙을 두 벌 만들지 않는다 — source 만 다르고 나머지는 POST 와 같다.

        여기서 안전망은 **진짜로 돈다**: 실제 어댑터가 가짜 드라이버에게 묻고, 실제 strict
        캡처러가 판정하고, 실제 파서가 그 화면을 읽는다.
        """
        driver = _FakeDriver(served="2026.08.27", html=_official_html())
        parent, decision, _ = self._run_flow(
            http={"side_effect": ValueError("계약 오류")}, driver=driver,
            sink=lambda n: True)

        self.assertEqual(driver.gets, 1, "안전망이 실제로 항해했다")
        self.driver_opens.assert_called_once()
        self.assertEqual(self._saved_rates(), RATES)
        self.assertIs(decision.result.status, IbkStatus.OBSERVED)
        self.assertIs(decision.result.source, IbkSource.OFFICIAL_SELENIUM,
                      "검증 통과분은 source 만 다르다")
        self.assertEqual(decision.result.changed_count, 3)
        self._assert_agrees_with_db(decision.result)
        self.assertEqual(parent.counts["OBSERVED"], 1)

    def test_a_historical_candidate_keeps_an_already_set_official_clock(self):
        """⛔ 과거 후보를 당일 관측으로 승격하지 않는다. 그리고 **이미 설정된** 최신 공식
        시각을 되돌리지도 않는다 — None 만 보면 초기화 회귀를 놓친다."""
        tried = []

        def post(url, **kwargs):
            asked = kwargs["data"]["inDate"]
            tried.append(asked)
            if asked == SERVICE_DATE.strftime("%Y.%m.%d"):
                return _http_response(_no_session_html(asked))
            return _http_response(_official_html(selected_date=asked))

        self._seed(OTHER, -3600)          # 보존 판정이 성립하려면 기존 값이 있어야 한다
        parent, decision, _ = self._run_flow(http={"side_effect": post},
                                             sink=lambda n: True,
                                             seed_official_clock="2026-08-28T00:00:00+00:00")

        self.assertIs(decision.result.status, IbkStatus.PRESERVED)
        self.assertEqual(parent.counts["PRESERVED"], 1)
        self.assertEqual(parent.last_current_official_at, "2026-08-28T00:00:00+00:00",
                         "과거 후보는 이미 설정된 최신 공식 시각을 바꾸지 않는다")
        self.assertGreater(len(tried), 1, "양성 대조 — 더 과거 날짜를 실제로 시도했다")
        self._assert_agrees_with_db(decision.result)


class ForbiddenCallsAreCountedNotGuarded(EndToEndTest):
    """축 2 — 거부·예산 소진에서 브라우저와 MIBANK 를 실제로 부르지 않는가.

    ⛔ 예외를 던지는 가드만으로는 부족하다. 안전망의 바깥 `except Exception` 이 그
       `AssertionError` 를 삼켜 `driver_failed:AssertionError` 라는 **관측 결과**로 바꾸므로,
       가드가 발화해도 흐름은 계속되고 시험은 통과한다. 그래서 횟수를 센다.
    """

    def test_a_semantic_rejection_opens_no_browser_and_no_mibank(self):
        """무고시는 정상 관측이다 — 더 비싼 경로를 열 이유가 없다."""
        parent, decision, _ = self._run_flow(
            http={"side_effect": lambda url, **kw:
                  _http_response(_no_session_html(kw["data"]["inDate"]))},
            sink=lambda n: True)

        self.driver_opens.assert_not_called()
        self.mibank.assert_not_called()
        self.assertEqual(parent.counts[decision.result.status.value], 1)
        self.assertIn(decision.result.status, (IbkStatus.PRESERVED, IbkStatus.FAILED))

    def test_an_exhausted_budget_opens_no_browser_and_no_mibank(self):
        """⛔ 예산이 없어 멈춘 자리에서 더 비싼 경로를 여는 것은 모순이다."""
        parent, decision, _ = self._run_flow(
            http={"side_effect": AssertionError("예산이 없으면 요청도 하지 않는다")},
            sink=lambda n: True, monotonic_offset=-120.0)

        self.driver_opens.assert_not_called()
        self.mibank.assert_not_called()
        self.assertIs(decision.result.reason, IbkReason.BUDGET_EXHAUSTED)
        self.assertEqual(self._rows(), 0, "예산 소진 회차는 아무것도 저장하지 않는다")

    def test_a_technical_failure_that_cannot_fund_selenium_opens_no_browser(self):
        """진입 예산 미달 — 안전망이 드라이버를 만들지 않고 HTTP 진단을 남긴다."""
        parent, decision, _ = self._run_flow(
            http={"side_effect": ValueError("계약 오류")}, sink=lambda n: True,
            timeout=12)                       # 작업 예산 1초 — 안전망 진입 12초에 못 미친다

        self.driver_opens.assert_not_called()
        self.mibank.assert_not_called()
        self.assertIs(decision.result.reason, IbkReason.CONTRACT_ERROR,
                      "돌지 못한 안전망은 HTTP 진단을 덮지 않는다")


class NoticeAcceptanceAndDeliveryAreSeparate(EndToEndTest):
    """축 3 — 큐 접수와 실제 발송, 그리고 최종 실패가 구분되는가."""

    def _failing_run(self, *, send):
        delivery = self._delivery(send=send)
        parent, decision, _ = self._run_flow(
            http={"side_effect": ValueError("계약 오류")}, sink=delivery.event_sink)
        return delivery, parent, decision

    def test_a_failing_run_is_enqueued_and_actually_sent(self):
        sent = []
        delivery, parent, decision = self._failing_run(
            send=lambda text: sent.append(text) or True)

        self.assertEqual(parent.notice_enqueued, 1, "접수는 부모가 센다")
        self.assertTrue(self._await(lambda: delivery.stats()["sent"] == 1),
                        f"발송이 일어나지 않았다: {delivery.stats()}")
        self.assertEqual(len(sent), 1)
        self.assertIn(decision.result.reason.value, sent[0],
                      "경보 본문에 판정 사유가 들어가야 한다")

    def test_acceptance_is_not_delivery(self):
        """⛔ 접수와 발송을 같은 수로 세면 '큐에 넣었다' 가 '보냈다' 로 읽힌다."""
        delivery, parent, _ = self._failing_run(send=lambda text: False)

        self.assertEqual(parent.notice_enqueued, 1, "접수는 성공했다")
        self.assertEqual(parent.notice_enqueue_failed, 0)
        # ⛔ 계수를 먼저 올리고 unsent 를 뒤에 기록한다(ibk_notice_delivery.py). 계수만 기다린
        #    뒤 unsent 를 단언하면 간헐 실패한다 — **둘을 함께** 기다린다.
        self.assertTrue(
            self._await(lambda: delivery.stats()["send_failed_final"] == 1
                        and len(delivery.unsent()) == 1),
            f"최종 실패로 끝나야 한다: {delivery.stats()} / unsent={delivery.unsent()}")
        self.assertEqual(delivery.stats()["sent"], 0, "보내지 못했다")
        self.assertEqual(delivery.unsent()[0]["outcome"], "send_failed_final")

    def test_an_inactive_delivery_is_recorded_as_enqueue_failure(self):
        """전달이 꺼져 있으면 부모는 **접수 실패**로 센다 — 억제 상태로 넘어가지 않는다."""
        delivery = IbkNoticeDelivery(send=lambda text: True, enabled=False)
        parent, _, _ = self._run_flow(http={"side_effect": ValueError("계약 오류")},
                                      sink=delivery.event_sink)

        self.assertEqual(parent.notice_enqueued, 0)
        self.assertEqual(parent.notice_enqueue_failed, 1)
        self.assertEqual(delivery.stats()["rejected_inactive"], 1)


class ARealStrictRejectionReachesTheParentAndTheAlert(EndToEndTest):
    """축 1·3 교차 — 안전망이 **실제로 거부한** 관측이 저장을 막고 부모·경보까지 가는가.

    ⛔ 정상 관측만 통합으로 확인하면 "거부가 저장을 막는다" 는 이 슬라이스의 존재 이유가
       끝까지 이어지는지 모른다. 거부 화면도 **실제 파서·strict 판정**으로 만든다 —
       `REJECTED` 캡처를 손으로 만들지 않는다.
    ⛔ 완전 DB 는 `DEGRADED`, 부분 DB 는 `FAILED` 다. 프로토콜이 `FAILED` +
       `SELENIUM_STRICT_REJECTED` + 완전 DB 조합을 `INVALID_REJECTED_STATUS` 로 거부하므로
       (ibk_result_protocol.py), 이 둘은 바꿔 쓸 수 없다.
    """

    def _reject_flow(self, sent):
        delivery = self._delivery(send=lambda text: sent.append(text) or True)
        driver = _FakeDriver(served="2026.08.27",
                             html=_rejected_html("2026.08.27"))
        self.rows_before = self._all_rows()          # 실행 **전** 행 전체
        return self._run_flow(http={"side_effect": ValueError("계약 오류")},
                              driver=driver, sink=delivery.event_sink) + (delivery,)

    def _assert_common(self, parent, decision, sent, delivery, *, before):
        self.assertIs(decision.result.reason, IbkReason.SELENIUM_STRICT_REJECTED)
        self.assertIsNone(decision.result.source, "거부된 관측은 출처를 주장하지 않는다")
        self.assertEqual(self._db_pairs(), before, "거부는 DB 를 바꾸지 않는다")
        self.assertEqual(self._all_rows(), self.rows_before,
                         "행이 하나도 바뀌지 않아야 한다 — 같은 값 중복 INSERT 도 안 된다")
        self.assertFalse(decision.should_retry, "의미 있는 판정은 재시도 대상이 아니다")
        self.mibank.assert_not_called()
        self.assertEqual(parent.notice_enqueued, 1, "경보를 접수했다")
        self.assertTrue(self._await(lambda: delivery.stats()["sent"] == 1),
                        f"발송이 일어나지 않았다: {delivery.stats()}")
        self.assertEqual(len(sent), 1, "발송기를 한 번 불렀다")
        self.assertIn("SELENIUM_STRICT_REJECTED", sent[0])
        self._assert_agrees_with_db(decision.result)

    def test_a_rejection_with_a_complete_db_is_degraded(self):
        self._seed(OTHER, -3600)
        sent = []
        parent, decision, _, delivery = self._reject_flow(sent)

        self.assertIs(decision.result.status, IbkStatus.DEGRADED)
        self.assertTrue(decision.result.db_snapshot_complete)
        self._assert_common(parent, decision, sent, delivery, before=OTHER)
        self.assertEqual(parent.counts["DEGRADED"], 1)
        self.assertIsNone(parent.last_current_official_at,
                          "거부된 관측은 최신 공식 시각을 앞당기지 않는다")

    def test_a_rejection_with_a_partial_db_is_failed(self):
        self._seed({"usd-krw": OTHER["usd-krw"]}, -3600)
        sent = []
        parent, decision, _, delivery = self._reject_flow(sent)

        self.assertIs(decision.result.status, IbkStatus.FAILED)
        self.assertFalse(decision.result.db_snapshot_complete)
        self._assert_common(parent, decision, sent, delivery,
                            before={"usd-krw": OTHER["usd-krw"]})
        self.assertEqual(parent.counts["FAILED"], 1)


class AReturnedResultIsNotATimeout(EndToEndTest):
    """축 4 — 자식이 결과를 돌려준 것과 부모가 시간 초과로 끝난 것이 구분되는가."""

    def test_a_returned_result_is_a_result(self):
        parent, decision, _ = self._run_flow(
            http={"return_value": _http_response(_official_html())}, sink=lambda n: True)

        self.assertIsNotNone(decision.result, "프레임이 왔으면 결과다")
        self.assertIs(decision.result.status, IbkStatus.OBSERVED)
        self.assertEqual(parent.counts["OBSERVED"], 1)
        self.assertEqual(self._rows(), 3, "저장도 실제로 일어났다")

    def test_a_completed_frame_under_a_timeout_is_still_a_timeout(self):
        """⛔ 자식이 프레임을 **다 쓰고도** 부모가 기다리다 끊는 경우가 이 축의 관심사다.

        ⛔ stdout 을 비우고 returncode 를 1 로 두면 `timed_out` 없이도 같은 결과가 나와
           신호가 격리되지 않는다 — 그렇게 만든 첫 판은 `timed_out` 분기를 제거한 변이가
           **생존**했다(실측). 그래서 완주한 프레임과 종료 코드 0 을 그대로 주고 시간 초과만
           켠다.
        ⛔ 자식은 같은 일을 다 했다 — 저장까지 일어났다. 달라진 것은 부모가 그것을 결과로
           받았는지뿐이고, 부모의 상태는 그 차이를 드러내야 한다.
        """
        parent, decision, frame = self._run_flow(
            http={"return_value": _http_response(_official_html())}, sink=lambda n: True,
            timed_out=True)

        self.assertTrue(frame.payload, "양성 대조 — 자식은 완주한 프레임을 냈다")
        self.assertEqual(frame.code, 0, "양성 대조 — 자식은 정상 종료했다")
        self.assertEqual(self._rows(), 3, "양성 대조 — 자식의 저장은 일어났다")
        self.assertIsNone(decision.result, "시간 초과는 의미 있는 판정이 아니다")
        # ⛔ "결과가 아니다" 만 보면 시간 초과와 프로세스 오류를 구분하지 못한다. 부모가
        #    계수하는 이름까지 본다 — 그러지 않으면 오분류 변이가 지나간다.
        self.assertIs(decision.failure, IbkExecutionFailure.PROCESS_TIMEOUT)
        self.assertEqual(parent.counts[IbkExecutionFailure.PROCESS_TIMEOUT.value], 1)
        self.assertEqual(parent.counts["OBSERVED"], 0)
        self.assertIsNone(parent.last_current_official_at)


if __name__ == "__main__":
    unittest.main()
