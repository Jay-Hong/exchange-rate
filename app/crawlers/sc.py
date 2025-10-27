# app/crawlers/sc.py

# 표준 라이브러리
import datetime
import logging
import time

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal
from app.crawlers.constants import HEADERS, DEFAULT_TIMEOUT, SELENIUM_WAIT_TIMEOUT
from app.crawlers.utils import parse_rate_text, create_selenium_driver, is_mibank_rate_reliable

BANK_NAME = 'sc'

SC_BANK_URL = 'https://www.standardchartered.co.kr/np/kr/pl/pn/ForeignExchange.jsp'    # 자정이후, 주말에는 환율정보 제공안함
SC_BANK_SELECTORS = {
    'usd-krw': '#tdUSD',
    'jpy-krw': '#tdJPY',
    'eur-krw': '#tdEUR',
    # 'cny-krw': 'tdCNY',
}

SECOND_SC_BANK_URL = 'https://www.standardchartered.co.kr/np/kr/pl/et/ExchangeRateP1.jsp'   # 자정이후, 주말에는 날짜변경 후 조회
SECOND_SC_BANK_SELECTOR = '#TMP_RATE' # usd-krw, jpy-krw, eru-krw 모두 selector 같음 (2,3,4번째 값)
SECOND_SC_BANK_PAIRS = ['usd-krw', 'jpy-krw', 'eur-krw']
MAX_DAYS_LOOKBACK = 12  # 최대 조회 가능한 과거 날짜 수
# SC제일은행 날짜 선택 selector (년/월/일 select 박스)
YEAR_SELECTOR = "#_CUR_YEAR"
MONTH_SELECTOR = "#_CUR_MONTH"
DAY_SELECTOR = "#_CUR_DAY"
SUBMIT_BUTTON_SELECTOR = "input[type='button'][value='조회 시작일']"  # 녹색 조회 버튼

MIBANK_SC_CODE = '023'
MIBANK_SC_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + MIBANK_SC_CODE
MIBANK_SELECTORS = {
    'usd-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(3) > td.right.counter.rollsty01',
    'jpy-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(2) > td.right.counter.rollsty01',
    'eur-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(4) > td.right.counter.rollsty01',
    # 'cny-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(1) > td.right.counter.rollsty01',
}


# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")

def crawl_and_save_sc_bank_exchange_rates():
    """SC제일은행 환율 크롤링"""
    db = SessionLocal()
    try:
        crawl_and_save_routine_selenium(SC_BANK_URL, SC_BANK_SELECTORS, db)
    except Exception as e:
        # 아래를 logger.exception으로 하지 않은이유 : 자정 이후/주말에는 이 URL이 안됨
        logger.info("SC_BANK_URL 크롤링 실패", extra={"url": SECOND_SC_BANK_URL})
        try:
            logger.info("SECOND_SC_BANK_URL 시도")
            # 아래를 두번째로 시도하는 이유 : Main으로 두었을때 가끔 환율조회가 안되어 - '#TMP_RATE' selector가 하나만 나타나 - 전날 환율이 저장 됨
            crawl_and_save_sc_second_routine_selenium(SECOND_SC_BANK_URL, SECOND_SC_BANK_SELECTOR, db)
        except Exception as e:
            logger.exception("SECOND_SC_BANK_URL 크롤링 실패", extra={"url": SC_BANK_URL})

            # 3차 시도: MIBANK (자정/주말 차단, 일반 공휴일은 고려하지 못함)
            if is_mibank_rate_reliable():
                try:
                    logger.info("MIBANK_SC_URL 시도 (평일 09:00 ~ 24:00 / 자정,주말 제외)")
                    crawl_and_save_routine(MIBANK_SC_URL, MIBANK_SELECTORS, db)
                except Exception as e2:
                    logger.exception("MIBANK_SC_URL 크롤링 실패", extra={"url": MIBANK_SC_URL})
                    error_msg = f"모든 URL 실패: {str(e2)[:100]}"
                    logger.error(f"❌ {BANK_NAME} 크롤링 실패 (모든 URL)", extra={"error": error_msg})
            else:
                logger.warning(
                    f"⏰ MIBANK - {BANK_NAME} - 크롤링 건너뜀 (자정/주말 + Selenium 실패)",
                    extra={
                        "reason": "is_mibank_rate_reliable & selenium failed",
                        "action": "DB 마지막 환율 데이터 유지 (클라이언트가 재사용)"
                    }
                )
                # 아무것도 하지 않음 → DB에 INSERT 없음 → 클라이언트가 마지막 SC 환율 표시
    finally:
        db.close()    


def crawl_and_save_routine_selenium(url: str, selectors: dict, db: Session) -> int:
    """Selenium 크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    driver = create_selenium_driver()

    current_rates = {}
    try:
        driver.get(url) # url 오류면 여기서 에러남
        wait = WebDriverWait(driver, SELENIUM_WAIT_TIMEOUT)

        for pair, selector in selectors.items():
            try:
                rate_element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                rate_text = rate_element.text.strip()
            except Exception as e:
                logger.warning(f"⚠️ SELECTOR 오류: {pair}", extra={"pair": pair, "selector": selector, "bank": BANK_NAME})
                continue

            try:
                current_rate = parse_rate_text(rate_text)
                current_rates[pair] = current_rate
            except ValueError:
                # 아래를 logger.warning으로 하지 않은이유 : 자정 이후/주말에는 이 URL이 안됨
                logger.info(f"⚠️ 유효하지 않은 환율: {pair}", extra={"pair": pair, "rate_text": rate_text, "bank": BANK_NAME})
                continue

        # db 저장
        if current_rates:
            return crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)
        else:
            raise Exception(f"환율 데이터 추출 실패 (셀렉터 오류 또는 데이터 없음)")

    except Exception as e:
        error_msg = str(e)
        if "환율 데이터 추출 실패" in error_msg:
            # 아래를 logger.exception으로 하지 않은이유 : 자정 이후/주말에는 이 URL이 안됨
            logger.info(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception",
                extra={"url": url, "bank": BANK_NAME, "selectors": list(selectors.keys()), "error": error_msg})
        else:
            logger.exception("⚠️ URL 접속 또는 처리 오류",
                extra={"url": url, "bank": BANK_NAME, "error": error_msg})
        raise
    finally:
        driver.quit()


# SC제일은행 전용함수 for "https://www.standardchartered.co.kr/np/kr/pl/et/ExchangeRateP1.jsp"
def crawl_and_save_sc_second_routine_selenium(url: str, selector: str, db: Session) -> int:
    """SC제일은행 Selenium 크롤링 (날짜 변경 지원)

    - 자정 이후/주말: #TMP_RATE selector 하나만 있거나 없음 → 과거 날짜로 조회 (최대 12일)
    """
    driver = create_selenium_driver()

    current_rates = {}
    try:
        driver.get(url) # url 오류면 여기서 에러남
        wait = WebDriverWait(driver, SELENIUM_WAIT_TIMEOUT)

        # Alert 처리 (자정 이후/주말에 뜨는 메시지)
        try:
            alert = driver.switch_to.alert
            alert_text = alert.text
            logger.debug(f"Alert 감지: {alert_text}", extra={"bank": BANK_NAME})
            alert.accept()
        except Exception:
            pass  # Alert 없으면 무시

        # #TMP_RATE selector 존재 여부 및 개수 확인
        try:
            rate_elements = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector)))
            element_count = len(rate_elements)

            # 여러 개 존재하면 평일 영업시간 → 현재 날짜 환율 크롤링
            if element_count > 1:
                logger.debug(f"✅ {BANK_NAME} 현재 날짜 환율 존재 (요소 {element_count}개)", extra={"bank": BANK_NAME, "element_count": element_count})
                current_rates = crawl_current_date_rates(driver, wait, selector)
            else:
                # 1개만 있으면 자정 이후/주말 → 과거 날짜로 조회
                logger.info(f"📅 {BANK_NAME} 과거 날짜 조회 시작 (#TMP_RATE 1개만 존재 - 자정/주말)", extra={"bank": BANK_NAME})
                current_rates = crawl_past_date_rates(driver, wait, selector)

        except Exception as e:
            # Selector가 아예 없으면 과거 날짜로 조회
            logger.info(f"📅 {BANK_NAME} 과거 날짜 조회 시작 (현재 날짜 환율 없음)", extra={"bank": BANK_NAME})
            current_rates = crawl_past_date_rates(driver, wait, selector)
        # db 저장
        if current_rates:
            return crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)
        else:
            raise Exception(f"환율 데이터 추출 실패 (셀렉터 오류 또는 데이터 없음)")

    except Exception as e:
        error_msg = str(e)
        if "환율 데이터 추출 실패" in error_msg:
            logger.error(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception",
                extra={"url": url, "bank": BANK_NAME, "selector": selector, "error": error_msg})
        else:
            logger.exception("⚠️ URL 접속 또는 처리 오류",
                extra={"url": url, "bank": BANK_NAME, "error": error_msg})
        raise
    finally:
        driver.quit()


def crawl_current_date_rates(driver, wait, selector: str) -> dict:
    """현재 날짜 환율 크롤링 (평일 09:00~24:00)"""
    current_rates = {}
    try:
        rate_elements = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector)))
        for index, rate_element in enumerate(rate_elements):
            if index in (1, 2, 3):  # usd-krw 2, jpy-krw 3, eur-krw 4 번째 값
                rate_text = rate_element.text.strip()
                try:
                    current_rate = parse_rate_text(rate_text)
                    current_rates[SECOND_SC_BANK_PAIRS[index-1]] = current_rate
                except ValueError:
                    logger.warning(f"⚠️ 유효하지 않은 환율: {SECOND_SC_BANK_PAIRS[index-1]}",
                        extra={"pair": SECOND_SC_BANK_PAIRS[index-1], "rate_text": rate_text, "bank": BANK_NAME})
                    continue
    except Exception as e:
        logger.warning(f"⚠️ SELECTOR 오류: {selector}", extra={"selector": selector, "bank": BANK_NAME})

    return current_rates


def crawl_past_date_rates(driver, wait, selector: str) -> dict:
    """과거 날짜 환율 크롤링 (어제부터 시작)

    호출 조건: 오늘 날짜에 #TMP_RATE가 1개만 존재 (데이터 없음)
    동작: 어제(today-1)부터 최대 MAX_DAYS_LOOKBACK일 전까지 순회
    """
    current_rates = {}
    selected_date = datetime.date.today()

    for i in range(MAX_DAYS_LOOKBACK):
        # 어제부터 과거로 이동 (오늘은 이미 parent에서 확인)
        selected_date = selected_date - datetime.timedelta(days=1)
        logger.info(f"📅 날짜 변경 {selected_date.strftime('%Y.%m.%d')}",
            extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME})

        try:
            # 1. 날짜 선택 (년/월/일 select 박스)
            logger.debug(f"🔍 날짜 셀렉터 찾기", extra={"bank": BANK_NAME})

            year_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, YEAR_SELECTOR))))
            year_select.select_by_value(str(selected_date.year))

            month_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, MONTH_SELECTOR))))
            month_select.select_by_value(f"{selected_date.month:02d}")

            day_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, DAY_SELECTOR))))
            day_select.select_by_value(f"{selected_date.day:02d}")

            logger.debug(f"📅 날짜 선택 완료: {selected_date.strftime('%Y.%m.%d')}", extra={"bank": BANK_NAME})

            # 2. 조회 버튼 클릭
            submit_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.button[title='조회']")))
            logger.debug(f"🖱️ 조회 버튼 클릭", extra={"bank": BANK_NAME})
            submit_button.click()

            # 3. Alert 처리 (조회 직후, "0회차" 메시지 등)
            time.sleep(0.5)  # Alert이 뜨기까지 짧은 대기
            has_data = True
            try:
                alert = driver.switch_to.alert
                alert_text = alert.text
                logger.debug(f"🔔 조회 후 Alert 감지: {alert_text[:50]}...", extra={"bank": BANK_NAME})
                alert.accept()

                # "0회차" 메시지면 다음 날짜로
                if "0회차" in alert_text:
                    logger.debug(f"⚠️ 해당 날짜에 데이터 없음 (0회차)", extra={"bank": BANK_NAME})
                    has_data = False
            except Exception:
                pass  # Alert 없으면 정상

            # 데이터 없으면 다음 날짜로
            if not has_data:
                continue

            # 4. AJAX 대기 및 #TMP_RATE 확인
            logger.debug(f"⏳ AJAX 응답 대기 중...", extra={"bank": BANK_NAME})
            rate_elements = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector)))
            element_count = len(rate_elements)
            logger.debug(f"✅ AJAX 응답 완료 (요소 {element_count}개)", extra={"bank": BANK_NAME})

            # 5. 환율 크롤링
            if element_count > 1:
                current_rates = crawl_current_date_rates(driver, wait, selector)

                if current_rates:
                    logger.info(f"✅ {BANK_NAME} 환율 조회 성공 (날짜: {selected_date.strftime('%Y.%m.%d')}, 요소 {element_count}개)",
                        extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME, "element_count": element_count})
                    break  # 성공하면 종료
                else:
                    # 크롤링 실패 → 다음 날짜로
                    logger.debug(f"⚠️ 환율 데이터 파싱 실패", extra={"bank": BANK_NAME})
                    continue
            else:
                # 1개만 있으면 의미 없음 → 다음 날짜로
                logger.debug(f"⚠️ #TMP_RATE 1개만 존재", extra={"bank": BANK_NAME})
                continue

        except Exception as e:
            logger.warning(f"⚠️ 날짜 변경 실패: {type(e).__name__}",
                extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME, "error": str(e)[:200]})
            break

    return current_rates


def crawl_and_save_routine(url: str, selectors: dict, db: Session) -> int:
    """크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    current_rates = {}
    try:
        response = requests.get(url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        for pair, selector in selectors.items():
            rate_element = soup.select_one(selector)

            if not rate_element:
                logger.warning(f"⚠️ SELECTOR 오류: {pair}", extra={"pair": pair, "selector": selector, "bank": BANK_NAME})
                continue

            rate_text = rate_element.get_text(strip=True)

            try:
                current_rate = parse_rate_text(rate_text)
                current_rates[pair] = current_rate
            except ValueError:
                logger.warning(f"⚠️ 유효하지 않은 환율: {pair}", extra={"pair": pair, "rate_text": rate_text, "bank": BANK_NAME})
                continue

    except Exception as e:
        logger.exception("⚠️ URL 오류", extra={"url": url, "bank": BANK_NAME})
        raise

    # db 저장
    if current_rates:
        return crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)
    else:
        raise Exception(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception")
