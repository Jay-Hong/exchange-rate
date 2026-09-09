"""IBK strict 캡처가 쓰는 **실제 WebDriver 조작**. 흐름은 캡처러가 소유한다.

여기 있는 것은 "드라이버에게 무엇을 어떻게 묻는가" 뿐이다. 언제 묻는지·무엇을 판정하는지는
`app/ibk_selenium_strict.py` 가 정한다.

⛔ 서비스 날짜는 `element.get_attribute("value")` 로 읽으면 안 된다. Selenium 은 input 에서
   **property 를 우선 반환**하므로 입력만 되고 제출되지 않은 값이 나온다(로컬 실측: 입력 후
   property=2026.09.04 / 내용 속성=page_source=2026.09.09). 파서가 읽을 값은 **내용 속성**이다.
⛔ 제출 완료는 **문서 루트 교체 + 새 문서 준비**로 본다. 입력 요소만 보면 부분 갱신에서
   "요소가 떨어졌다" 를 "문서가 바뀌었다" 로 오독한다.
⛔ 두 대기는 **하나의 기한**을 나눠 쓴다. 각자 상한을 쓰면 예산이 두 배가 된다.
"""

import time

from selenium.common.exceptions import NoAlertPresentException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

#: 문서 루트. `staleness_of` 를 걸 대상이며 부분 갱신과 문서 교체를 가른다.
DOCUMENT_ROOT_SELECTOR = "html"
#: 준비 완료 판정. 교체된 문서가 읽을 수 있는 상태인지 본다.
READY_STATE_SCRIPT = "return document.readyState"
#: 내용 속성 읽기. property 가 아니라 서버가 렌더한 값이다.
SERVED_DATE_SCRIPT = "return arguments[0].getAttribute('value')"


def make_served_date(input_selector):
    def served_date(driver):
        node = driver.find_element(By.CSS_SELECTOR, input_selector)
        return driver.execute_script(SERVED_DATE_SCRIPT, node)
    return served_date


def make_find_input(input_selector):
    def find_input(driver):
        return driver.find_element(By.CSS_SELECTOR, input_selector)
    return find_input


def document_root(driver):
    return driver.find_element(By.CSS_SELECTOR, DOCUMENT_ROOT_SELECTOR)


def submit(element, text):
    element.clear()
    element.send_keys(text)
    element.send_keys(Keys.ENTER)


def take_alert(driver):
    """떠 있는 경고를 받아 문구를 돌려준다. 없으면 None."""
    try:
        alert = driver.switch_to.alert
        text = alert.text
        alert.accept()
        return text or "alert"
    except NoAlertPresentException:
        return None


def make_wait_replaced(driver, *, monotonic=None):
    """루트 교체와 준비 완료를 **하나의 기한** 안에서 확인한다.

    시간이 모자라면 예외가 아니라 `False` 를 돌려준다 — 캡처러가 그것을
    `submit_not_confirmed` 로 접어 `page_source` 를 읽지 않는다.

    ⛔ 시계는 **호출 시점에** 읽는다. `monotonic=time.monotonic` 처럼 기본 인자로 묶으면
       정의 시점의 함수가 박혀 패치가 듣지 않는다(이 리포에서 재발한 결함).
    ⛔ `WebDriverWait.until` 은 조건이 참이면 **시간 초과 검사 전에** 돌아올 수 있다.
       그래서 대기가 끝난 뒤 기한을 **다시** 본다 — 안 보면 기한 110 에 111 에서 True 가
       나온다(실측).
    """
    clock = monotonic or (lambda: time.monotonic())

    def wait_replaced(root, timeout):
        deadline = clock() + timeout
        try:
            WebDriverWait(driver, max(deadline - clock(), 0.0)).until(
                EC.staleness_of(root))
        except TimeoutException:
            return False
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        try:
            WebDriverWait(driver, remaining).until(
                lambda d: d.execute_script(READY_STATE_SCRIPT) == "complete")
        except TimeoutException:
            return False
        # 조건이 참이어도 기한을 넘겼으면 확인으로 치지 않는다.
        return clock() <= deadline
    return wait_replaced


def build_operations(driver, *, input_selector, read_page_source, monotonic=None):
    """캡처러 생성자에 그대로 넘길 조작 묶음.

    `read_page_source` 는 주입받는다 — 시간 제한이 걸린 읽기는 이미 크롤러 쪽에 있고,
    이 모듈이 그것을 다시 구현하면 두 벌이 갈라진다.
    """
    if not callable(read_page_source):
        raise ValueError("INVALID_IBK_ADAPTER_READ_PAGE_SOURCE")
    return {
        "read_page_source": read_page_source,
        "served_date": make_served_date(input_selector),
        "find_input": make_find_input(input_selector),
        "submit": submit,
        "document_root": document_root,
        "wait_replaced": make_wait_replaced(driver, monotonic=monotonic),
        "take_alert": take_alert,
    }
