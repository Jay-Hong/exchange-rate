# app/crawlers/ibk.py

# 표준 라이브러리
import datetime
import logging
import time

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal
from app.crawlers.constants import HEADERS, DEFAULT_TIMEOUT, SELENIUM_WAIT_TIMEOUT
from app.crawlers.utils import parse_rate_text, create_selenium_driver, is_mibank_rate_reliable

BANK_NAME = 'ibk'

IBK_BANK_URL = 'https://www.ibk.co.kr/fxtr/excRateList.ibk'    # 자정이후, 주말에는 Selenium으로 날짜변경 후 조회
IBK_BANK_SELECTORS = {
    'usd-krw': '#contents_in > div.section_last > div.table_view_section2 > table > tbody > tr:nth-child(1) > td:nth-child(3)',
    'jpy-krw': '#contents_in > div.section_last > div.table_view_section2 > table > tbody > tr:nth-child(2) > td:nth-child(3)',
    'eur-krw': '#contents_in > div.section_last > div.table_view_section2 > table > tbody > tr:nth-child(3) > td:nth-child(3)',
    # '#contents_in > div.section_last > div.table_view_section2 > table > tbody > tr:nth-child(4) > td:nth-child(3)',
}
INPUT_SELECTOR = "#inDate"
MAX_DAYS_LOOKBACK = 12  # 최대 조회 가능한 과거 날짜 수

MIBANK_IBK_CODE = '003'
MIBANK_IBK_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + MIBANK_IBK_CODE
MIBANK_SELECTORS = {
    'usd-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(3) > td.right.counter.rollsty01',
    'jpy-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(2) > td.right.counter.rollsty01',
    'eur-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(4) > td.right.counter.rollsty01',
    # 'cny-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(1) > td.right.counter.rollsty01',
}


# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")

def crawl_and_save_ibk_bank_exchange_rates():
    """기업은행 환율 크롤링"""
    db = SessionLocal()
    try:
        # 1차 시도: Requests (빠른 경로 - 평일 영업시간)
        if try_crawl_with_requests(db):
            logger.debug(f"✅ {BANK_NAME} Requests 크롤링 성공")
            return

        # 2차 시도: Selenium (날짜 변경 필요 - 자정/공휴일) - 최대 3회 재시도
        logger.info(f"➡️ {BANK_NAME} Selenium으로 전환 (환율 데이터 없음)")
        for attempt in range(3):
            try:
                crawl_and_save_ibk_routine_selenium(IBK_BANK_URL, IBK_BANK_SELECTORS, db)
                logger.info(f"✅ {BANK_NAME} Selenium 성공 (시도 {attempt+1}/3)")
                return  # 성공 시 종료
            except Exception as e:
                logger.warning(f"⚠️ {BANK_NAME} Selenium 실패 (시도 {attempt+1}/3): {str(e)[:50]}")
                if attempt < 2:  # 마지막 시도 전이면
                    time.sleep(2)  # 2초 대기 후 재시도
                else:
                    raise  # 3회 실패 시 예외 발생

    except Exception as e:
        logger.exception("IBK_BANK_URL 크롤링 실패", extra={"url": IBK_BANK_URL})

        # 3차 시도: MIBANK (자정/주말 차단, 일반 공휴일은 고려하지 못함 ← Selenium 3회 재시도로 커버)
        if is_mibank_rate_reliable():
            try:
                logger.info("MIBANK_IBK_URL 시도 (평일 09:00 ~ 24:00 / 자정,주말 제외)")
                crawl_and_save_routine(MIBANK_IBK_URL, MIBANK_SELECTORS, db)
            except Exception as e2:
                logger.exception("MIBANK_IBK_URL 크롤링 실패", extra={"url": MIBANK_IBK_URL})
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
            # 아무것도 하지 않음 → DB에 INSERT 없음 → 클라이언트가 마지막 IBK 환율 표시
    finally:
        db.close()    


def try_crawl_with_requests(db: Session) -> bool:
    """Requests로 IBK 크롤링 시도
    Returns: 성공 여부 (True: 성공, False: 실패)
    """
    try:
        response = requests.get(IBK_BANK_URL, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        # Selector 존재 여부 및 데이터 유효성 확인 (아래 구문 없으면 자정 이후와 주말에는 경고 계속 뜰 것)
        test_element = soup.select_one(IBK_BANK_SELECTORS['usd-krw'])
        if not test_element or not test_element.text.strip().replace(',', ''):
            logger.debug("IBK 환율 데이터 없음 (Selector 없음 또는 빈 데이터)")
            return False

        # 환율 크롤링
        current_rates = {}
        for pair, selector in IBK_BANK_SELECTORS.items():
            rate_element = soup.select_one(selector)
            if not rate_element:
                logger.warning(f"⚠️ SELECTOR 오류: {pair}",
                    extra={"pair": pair, "selector": selector, "bank": BANK_NAME})
                continue

            rate_text = rate_element.get_text(strip=True)
            if not rate_text or rate_text == '-':
                logger.debug(f"IBK 환율 데이터 없음: {pair} (빈 값 또는 '-')")
                continue

            try:
                current_rate = parse_rate_text(rate_text)
                current_rates[pair] = current_rate
            except ValueError:
                logger.warning(f"⚠️ 유효하지 않은 환율: {pair}",
                    extra={"pair": pair, "rate_text": rate_text, "bank": BANK_NAME})
                continue

        # DB 저장
        if current_rates:
            crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)
            return True
        else:
            logger.debug("IBK 환율 데이터 없음 (유효한 환율 없음)")
            return False

    except Exception as e:
        logger.debug(f"IBK Requests 크롤링 실패: {str(e)}")
        return False


def crawl_and_save_ibk_routine_selenium(url: str, selectors: dict, db: Session) -> int:
    """IBK 전용 Selenium 크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    driver = create_selenium_driver()

    current_rates = {}
    try:
        driver.get(url) # url 오류면 여기서 에러남
        wait = WebDriverWait(driver, SELENIUM_WAIT_TIMEOUT)

        selected_date = datetime.date.today()

        for i in range(MAX_DAYS_LOOKBACK):
            try:
                input_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, INPUT_SELECTOR)))
                test_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, IBK_BANK_SELECTORS['usd-krw'])))
                for pair, selector in IBK_BANK_SELECTORS.items():
                    try:
                        rate_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                        rate_text = rate_element.text.strip()
                    except Exception as e:
                        logger.warning(f"⚠️ SELECTOR 오류: {pair}", extra={"pair": pair, "selector": selector, "bank": BANK_NAME})
                        continue
                    try:
                        current_rate = parse_rate_text(rate_text)
                        current_rates[pair] = current_rate
                    except ValueError:
                        logger.warning(f"⚠️ 유효하지 않은 환율: {pair}", extra={"pair": pair, "rate_text": rate_text, "bank": BANK_NAME})
                        continue
                break   # 크롤링 되면 종료

            except Exception as e:
                selected_date = selected_date - datetime.timedelta(days=1)
                logger.info(f"📅 날짜 변경 {selected_date.strftime('%Y.%m.%d')}", extra={"selector": INPUT_SELECTOR, "bank": BANK_NAME})
                try:
                    input_element.clear()
                    input_element.send_keys(selected_date.strftime('%Y.%m.%d'))
                    input_element.send_keys(Keys.ENTER)
                except Exception as e:
                    logger.warning(f"⚠️ 날짜 변경 실패", extra={"selector": INPUT_SELECTOR, "bank": BANK_NAME})
                    break

        # db 저장
        if current_rates:
            return crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)
        else:
            raise Exception(f"환율 데이터 추출 실패 (셀렉터 오류 또는 데이터 없음)")

    except Exception as e:
        error_msg = str(e)
        if "환율 데이터 추출 실패" in error_msg:
            logger.error(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception",
                extra={"url": url, "bank": BANK_NAME, "selectors": list(selectors.keys()), "error": error_msg})
        else:
            logger.exception("⚠️ URL 접속 또는 처리 오류",
                extra={"url": url, "bank": BANK_NAME, "error": error_msg})
        raise
    finally:
        driver.quit()


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
