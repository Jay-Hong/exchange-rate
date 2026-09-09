"""IBK Selenium 보조 경로 — **저장하기 전에** 공식 계약을 검증한다.

기존 legacy 루틴은 고정 셀렉터로 읽어 **먼저 저장하고** shadow 가 켜져 있으면 저장 뒤에
관측한다. 여기서는 순서를 뒤집는다: 검증을 통과한 관측만 호출자에게 넘기고, 이 모듈은
DB 를 건드리지 않는다.

## 왜 날짜 readback 만으로는 부족한가

`page_source` 는 입력 property 를 반영하지 않아 `#inDate` 의 value **속성**에는 서버가
렌더한 날짜가 남는다(2026-09-09 실측). 그래서 "입력만 하고 제출되지 않은 화면"은 **다른**
날짜를 기대할 때 readback 불일치로 걸린다.

⛔ 그러나 **같은 날짜를 다시 조회하는 경우**는 걸리지 않는다 — 제출이 아예 일어나지 않아도
   화면이 이미 그 날짜를 서비스하고 있으면 readback 은 통과한다(실측). 따라서 제출 완료는
   **파싱 전에 따로 확인**해야 한다. 이 모듈은 그 순서를 강제한다.

## 연속 제출 가드

페이지 JS 가 문서 로드 시 `lastD = 현재시각 + 3초` 를 두고, 그보다 이른 제출에
`새로고침은 연속으로 할 수 없습니다` 경고 대화상자를 띄운다(2026-09-09 실측). 대화상자가
뜨면 `page_source` 를 포함한 후속 WebDriver 호출이 실패한다. 그래서 제출 전에 로드 이후
경과를 확인하고, 그래도 대화상자가 뜨면 **재시도하지 않고** 그 자리에서 접는다.

⛔ 의미적 거부(무고시·검증 실패) 뒤에 Chrome 을 다시 띄우지 않는다. 이 모듈은 드라이버를
   만들지도 닫지도 않고, 주어진 세션에서 한 번만 관측한다.
"""

import datetime
import math
import time
from dataclasses import dataclass, field

#: 페이지 JS 의 연속 제출 가드(로드 + 3초)보다 넉넉히 잡는다.
SUBMIT_GUARD_SECONDS = 3.5
#: 제출 뒤 문서 교체를 기다리는 상한.
STALENESS_TIMEOUT_SECONDS = 10.0
#: 이보다 오래된 문서에서는 읽지 않는다 — 날짜가 맞아도 값이 낡았을 수 있다.
MAX_PAGE_AGE_SECONDS = 60.0

ACCEPTED = "accepted"
NO_SESSION = "no_session"
REJECTED = "rejected"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class IbkSeleniumStrictCapture:
    """관측 결과. 저장 여부는 호출자가 정한다 — 이 값은 저장 승인이 아니다."""

    verdict: str
    service_date: datetime.date | None = None
    rates: dict | None = None
    completed_at: str | None = None
    reason: str | None = None
    submitted: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        """저장 후보로 쓸 수 있는가. 무고시·거부·불가는 모두 아니다."""
        return self.verdict == ACCEPTED and bool(self.rates)


class IbkSeleniumStrictCapturer:
    """드라이버 조작을 주입받아 흐름만 소유한다. selenium·bs4 를 임포트하지 않는다."""

    def __init__(self, *, parse, served_date, find_input, submit, document_root,
                 read_page_source, wait_replaced, take_alert,
                 monotonic=None, sleep=None,
                 submit_guard_seconds=SUBMIT_GUARD_SECONDS,
                 staleness_timeout=STALENESS_TIMEOUT_SECONDS,
                 max_page_age_seconds=MAX_PAGE_AGE_SECONDS,
                 read_timeout=None):
        # ⛔ 기본값을 `time.monotonic` 으로 묶으면 정의 시점의 함수가 박혀 모듈 패치가 듣지
        #    않는다. 주입이 없으면 **호출 시점에** 모듈 속성을 읽는다.
        monotonic = monotonic or (lambda: time.monotonic())
        sleep = sleep or (lambda seconds: time.sleep(seconds))
        deps = (parse, served_date, find_input, submit, document_root,
                read_page_source, wait_replaced, take_alert, monotonic, sleep)
        if not all(callable(dep) for dep in deps):
            raise ValueError("INVALID_IBK_SELENIUM_STRICT_DEPENDENCY")
        for name, value in (("SUBMIT_GUARD", submit_guard_seconds),
                            ("STALENESS_TIMEOUT", staleness_timeout),
                            ("MAX_PAGE_AGE", max_page_age_seconds)):
            # NaN 은 `value < 0` 을 통과하고, 그러면 아래 나이 비교가 **항상 거짓**이 되어
            # 아무리 낡은 문서도 받아들여진다(실측).
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"INVALID_IBK_SELENIUM_STRICT_{name}")
        self._parse, self._served_date = parse, served_date
        self._find_input, self._submit = find_input, submit
        self._document_root, self._read_page_source = document_root, read_page_source
        self._wait_replaced, self._take_alert = wait_replaced, take_alert
        self._monotonic, self._sleep = monotonic, sleep
        # 읽기 상한은 주입이 없으면 읽기 함수 자신의 기본값을 쓴다(None 허용). 0 은 설정
        # 오류다 — 읽지 않겠다는 뜻이 되어 관측이 조용히 사라진다.
        if read_timeout is not None and (
                type(read_timeout) not in (int, float)
                or not math.isfinite(read_timeout) or read_timeout <= 0):
            raise ValueError("INVALID_IBK_SELENIUM_STRICT_READ_TIMEOUT")
        self._submit_guard_seconds = submit_guard_seconds
        self._staleness_timeout = staleness_timeout
        self._max_page_age_seconds = max_page_age_seconds
        self._read_timeout = read_timeout

    def capture(self, driver, *, query_date, reference_time,
                page_loaded_at, document_ready_at=None,
                deadline=None) -> IbkSeleniumStrictCapture:
        """한 세션에서 한 번 관측한다. 어떤 경로로도 저장하지 않는다.

        `page_loaded_at` 은 **항해를 시작한** monotonic 시각이다 — 문서 나이의 기준이며,
        보수적이어야 하므로 로딩에 걸린 시간도 나이에 포함된다.

        `document_ready_at` 은 **문서가 준비된** 시각이다 — 연속 제출 가드의 기준이다.
        페이지 JS 가 `document.ready` 때 `lastD = 그때 + 3초` 를 두므로 가드는 **준비 시각**
        에서 재야 한다. 둘을 같은 값으로 쓰면 항해가 8초 걸렸을 때 가드가 이미 지난 것으로
        계산되어 **대기 0초로 제출**한다(실측). 생략하면 `page_loaded_at` 을 쓴다.
        """
        ready_at = page_loaded_at if document_ready_at is None else document_ready_at
        notes = []
        pending, failed = self._guarded("alert_check", self._take_alert, driver)
        if failed:
            return _unavailable(failed, notes)
        if pending:
            # 들어올 때 이미 떠 있던 대화상자. 이 세션의 DOM 을 신뢰할 수 없다.
            return _unavailable("alert_present", notes + [_short(pending)])

        stale_reason = self._document_age_reason(page_loaded_at)
        if stale_reason:
            return _unavailable(stale_reason, notes)
        # ⛔ 준비 시각도 쓰기 전에 본다. NaN·-inf 를 주면 가드 대기가 사라져 **대기 0초로
        #    제출**한다(실측). 항해보다 이른 준비 시각도 성립하지 않는다.
        # ⛔ 미래의 준비 시각을 받으면 가드가 그만큼 기다리려 한다 — 준비 4600·현재 1000 에서
        #    **3603.5초 대기**를 요청한 뒤에야 만료를 판정했다(실측).
        if (type(ready_at) not in (int, float) or not math.isfinite(ready_at)
                or not (page_loaded_at <= ready_at <= self._monotonic())):
            return _unavailable("document_ready_at_unusable", notes)

        # ⛔ 요소의 `get_attribute("value")` 를 쓰면 안 된다 — Selenium 은 input 에서
        #    **property 를 우선 반환**하므로, 입력만 되고 제출되지 않은 값이 그대로 나온다
        #    (로컬 실측: 입력 후 property=2026.09.04 / 내용 속성=page_source=2026.09.09).
        #    그러면 필요한 제출을 건너뛰고 뒤의 파서가 거부한다. 판단 근거는 **문서가 실제로
        #    서비스하는 날짜**(내용 속성)여야 하고, 그것이 곧 파서가 읽을 값이다.
        expected = query_date.strftime("%Y.%m.%d")
        served, failed = self._guarded("served_date_unreadable", self._served_date, driver)
        if failed:
            return _unavailable(failed, notes)
        served = (served or "").strip()

        submitted = False
        document_at = page_loaded_at
        if served != expected:
            outcome = self._submit_and_confirm(driver, expected, ready_at, notes,
                                               deadline=deadline)
            if isinstance(outcome, IbkSeleniumStrictCapture):
                return outcome
            submitted, document_at = True, outcome
        else:
            # ⛔ 제출을 건너뛴다. 이 경로의 신선도 근거는 오직 호출자의 항해와 문서 나이
            #    검사다 — readback 은 여기서 아무것도 증명하지 못한다.
            notes.append("served_date_already_matched")

        # ⛔ 진입 때 한 번 본 것으로는 부족하다. 요소 탐색·제출·교체 대기 동안 시간이
        #    흐르므로 **읽기 직전에 다시** 본다(진입 후 61초를 흘려 accepted 가 나온 실측).
        stale_reason = self._document_age_reason(document_at)
        if stale_reason:
            return _unavailable(stale_reason, notes, submitted=submitted)

        # ⛔ 읽기에 **주는 시간**도 공유 기한을 따른다. 시작 여부만 검사하면 잔여 0.2초에서도
        #    상한 3초를 그대로 배정해 기한을 2.8초 넘겨 읽는다(실측).
        # ⛔ `page_source` 는 드라이버 명령 제한(page load·script)이 묶지 않는다. 그래서 이
        #    상한이 **호출자가 기다리는 시간**의 유일한 경계다 — 상한을 넘긴 읽기 스레드는
        #    daemon 으로 남고 WebDriver 명령 자체는 취소되지 않는다. 막는 것은 캡처가 그만큼
        #    붙잡혀 있는 것이지 명령의 종료가 아니다.
        # ⛔ 잔여를 **한 번 읽어** 만료 검사와 예산 계산에 함께 쓴다. 각각 읽으면 그 사이
        #    시간이 흘러 음수 예산이 전달된다.
        read_budget = self._read_timeout
        if deadline is not None:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return _unavailable("deadline_passed:before_read", notes,
                                    submitted=submitted)
            read_budget = remaining if read_budget is None else min(read_budget, remaining)
        read, failed = self._guarded("page_source", self._read_page_source, driver,
                                     timeout=read_budget)
        if failed:
            return _unavailable(failed, notes, submitted=submitted)
        html, read_failure = read
        if read_failure is not None:
            return IbkSeleniumStrictCapture(
                UNAVAILABLE, reason=read_failure, submitted=submitted, notes=tuple(notes))

        # 읽기 자체가 오래 걸렸으면 그 관측은 허용 창 밖이다.
        stale_reason = self._document_age_reason(document_at)
        if stale_reason:
            return _unavailable(f"{stale_reason}_after_read", notes, submitted=submitted)

        try:
            parsed = self._parse(html, query_date=query_date, reference_time=reference_time)
        except ValueError as exc:
            return IbkSeleniumStrictCapture(
                REJECTED, reason=_short(str(exc)), submitted=submitted, notes=tuple(notes))
        except Exception as exc:  # 관측이 수집 흐름을 예외로 깨뜨리지 않는다
            return IbkSeleniumStrictCapture(
                UNAVAILABLE, reason=f"{type(exc).__name__}", submitted=submitted,
                notes=tuple(notes))

        if parsed is None:
            return IbkSeleniumStrictCapture(
                NO_SESSION, service_date=query_date, submitted=submitted, notes=tuple(notes))
        rates, completed_at = parsed
        return IbkSeleniumStrictCapture(
            ACCEPTED, service_date=query_date, rates=dict(rates), completed_at=completed_at,
            submitted=submitted, notes=tuple(notes))

    def _guarded(self, label, operation, *args, **kwargs):
        """드라이버 조작을 감싼다. 예외는 **종류만** 남기고 결과로 바꾼다.

        ⛔ 원문을 그대로 옮기지 않는다 — 드라이버 예외에는 세션 주소 같은 환경 정보가 섞인다.
        """
        try:
            return operation(*args, **kwargs), None
        except Exception as exc:
            return None, f"{label}:{type(exc).__name__}"

    def _document_age_reason(self, since):
        """문서가 읽어도 되는 나이인지. 사유 문자열 또는 None."""
        now = self._monotonic()
        if not isinstance(since, (int, float)) or not math.isfinite(since):
            return "page_loaded_at_unusable"
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            return "clock_unusable"
        age = now - since
        if age < 0:
            return "clock_went_backwards"
        if age > self._max_page_age_seconds:
            return "page_too_old"
        return None

    def _submit_and_confirm(self, driver, expected, document_ready_at, notes,
                            *, deadline=None):
        """제출하고 **문서가 실제로 교체됐는지** 확인한다.

        성공하면 **제출 직전** monotonic 시각을 돌려준다. 새 문서는 제출 이후에 생기므로
        이 기준은 나이를 실제보다 작게 계산하지 않는다.

        ⛔ 교체가 **확인된** 시각을 쓰면 안 된다 — 문서가 생긴 시각과 확인이 반환된 시각은
           다르다. 확인이 70초 늦게 돌아오면 이미 70초 된 문서의 나이가 0 으로 재설정된다
           (실측). 상한을 넘긴 가짜 어댑터에서만 생기는 문제가 아니다.
        """
        # ⛔ 교체 확인의 기준은 **제출 전 문서의 루트**다. 입력 요소만 보면 부분 갱신에서
        #    "요소가 떨어졌다" 를 "문서가 바뀌었다" 로 오독한다.
        root, failed = self._guarded("document_root", self._document_root, driver)
        if failed:
            return _unavailable(failed, notes)
        if root is None:
            return _unavailable("document_root_absent", notes)

        element, failed = self._guarded("find_input", self._find_input, driver)
        if failed:
            return _unavailable(failed, notes)
        if element is None:
            return _unavailable("input_absent", notes)

        waited = self._submit_guard_seconds - (self._monotonic() - document_ready_at)
        if deadline is not None and waited > 0 and self._monotonic() + waited >= deadline:
            # 필요한 가드 대기가 남은 예산을 넘는다 — 기다리지 않고 접는다.
            return _unavailable("deadline_passed:before_guard", notes)
        if waited > 0:
            # 페이지 JS 의 연속 제출 가드. 기다리지 않으면 경고 대화상자로 세션이 막힌다.
            self._sleep(waited)
            notes.append(f"submit_guard_waited={waited:.2f}s")

        # 새 문서 나이의 보수적 기준. 문서는 이 시점 **이후**에 생긴다.
        submitted_at = self._monotonic()
        # ⛔ 가드 대기가 기한을 넘길 수 있다 — 대기 **뒤에** 다시 본다. 안 보면 기한이 지난
        #    뒤에도 제출하고 읽는다(실측: 기한 1000 에 1002.5 에서 제출·읽기 후 accepted).
        if deadline is not None and self._monotonic() >= deadline:
            return _unavailable("deadline_passed:before_submit", notes)
        _, failed = self._guarded("submit_failed", self._submit, element, expected)
        if failed:
            return _unavailable(failed, notes)

        blocked, failed = self._guarded("alert_check", self._take_alert, driver)
        if failed:
            return _unavailable(failed, notes)
        if blocked:
            # ⛔ 재시도하지 않는다. 다시 치면 같은 가드에 또 걸리고 Chrome 왕복만 늘어난다.
            return IbkSeleniumStrictCapture(
                UNAVAILABLE, reason="submit_blocked_by_alert",
                notes=tuple(notes + [_short(blocked)]))

        # ⛔ 확인 대기도 공유 기한을 따른다. 제출이 늦게 반환하면 잔여가 없는데도 설정 상한
        #    10초를 그대로 넘겨 새 대기를 시작했다(실측).
        # ⛔ 잔여를 **한 번 읽어 그 값을 검사하고 재사용**한다. 검사와 계산에서 시계를 각각
        #    읽으면 그 사이 시간이 흘러 음수 예산이 전달된다 — `WebDriverWait(timeout=0)` 도
        #    조건을 한 번 평가하므로 어댑터의 하한이 그것을 막지 못한다.
        confirm_budget = self._staleness_timeout
        if deadline is not None:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return _unavailable("deadline_passed:before_confirm", notes)
            confirm_budget = min(confirm_budget, remaining)
        # 확인용 예산은 **하나**다. 루트 교체 대기와 새 문서 준비 대기가 각자 상한을 쓰면
        # 예산이 두 배가 된다 — 어댑터가 이 하나를 나눠 쓴다.
        replaced, failed = self._guarded("confirm_failed", self._wait_replaced,
                                         root, confirm_budget)
        if failed:
            return _unavailable(failed, notes)
        if not replaced:
            # ⛔ 여기서 멈추지 않으면 같은 날짜 재조회가 그대로 통과한다 — 제출이 일어나지
            #    않아도 화면이 이미 그 날짜를 서비스하면 readback 은 맞는다(실측).
            return IbkSeleniumStrictCapture(
                UNAVAILABLE, reason="submit_not_confirmed", notes=tuple(notes))
        return submitted_at


def _unavailable(reason, notes, *, submitted=False):
    return IbkSeleniumStrictCapture(UNAVAILABLE, reason=reason, submitted=submitted,
                                    notes=tuple(notes))


def _short(text, limit=120):
    return str(text).replace("\n", " ")[:limit]
