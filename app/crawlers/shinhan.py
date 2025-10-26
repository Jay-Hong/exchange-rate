# app/crawlers/shinhan.py

# 표준 라이브러리
import logging

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal
from app.crawlers.constants import HEADERS, DEFAULT_TIMEOUT, SELENIUM_OPTIONS
from app.crawlers.utils import parse_rate_text, create_selenium_driver

BANK_NAME = 'shinhan'

SHINHAN_BANK_URL = 'https://bank.shinhan.com/rib/easy/index.jsp#210501000000'
SHINHAN_BANK_SELECTORS = {
    'usd-krw': '#grd_list_1_cell_0_6 > nobr',
    'jpy-krw': '#grd_list_1_cell_1_6 > nobr',
    'eur-krw': '#grd_list_1_cell_2_6 > nobr',
    # 'cny-krw': '#grd_list_1_cell_11_6 > nobr',
}

SECOND_SHINHAN_BANK_URL = 'https://bank.shinhan.com/index.jsp#020501010100'
SECOND_SHINHAN_BANK_SELECTORS = {
    'usd-krw': '#grd_list_1_cell_0_2 > nobr',
    'jpy-krw': '#grd_list_1_cell_1_2 > nobr',
    'eur-krw': '#grd_list_1_cell_2_2 > nobr',
    # 'cny-krw': '#grd_list_1_cell_11_2 > nobr',
}

MIBANK_SHINHAN_CODE = '088'
MIBANK_SHINHAN_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + MIBANK_SHINHAN_CODE
MIBANK_SELECTORS = {
    'usd-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(3) > td.right.counter.rollsty01',
    'jpy-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(2) > td.right.counter.rollsty01',
    'eur-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(4) > td.right.counter.rollsty01',
    # 'cny-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(1) > td.right.counter.rollsty01',
}

# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")

def crawl_and_save_shinhan_bank_exchange_rates():
    """신한은행 환율 크롤링"""
    db = SessionLocal()
    try:
        crawl_and_save_routine_selenium(SHINHAN_BANK_URL, SHINHAN_BANK_SELECTORS, db)
    except Exception as e:
        logger.exception("SHINHAN_BANK_URL 크롤링 실패", extra={"url": SHINHAN_BANK_URL})
        try:
            logger.info("SECOND_SHINHAN_BANK_URL 시도")
            crawl_and_save_routine_selenium(SECOND_SHINHAN_BANK_URL, SECOND_SHINHAN_BANK_SELECTORS, db)
        except Exception as e:
            logger.exception("SECOND_SHINHAN_BANK_URL 크롤링 실패", extra={"url": SECOND_SHINHAN_BANK_URL})
            try:
                logger.info("MIBANK_SHINHAN_URL 시도")
                crawl_and_save_routine(MIBANK_SHINHAN_URL, MIBANK_SELECTORS, db)
            except Exception as e:
                logger.exception("MIBANK_SHINHAN_URL 크롤링 실패", extra={"url": MIBANK_SHINHAN_URL})
                error_msg = f"모든 URL 실패: {str(e)[:100]}"
                logger.exception(f"❌ {BANK_NAME} 크롤링 실패 (모든 URL)", extra={"error": error_msg})
    finally:
        db.close()    


def crawl_and_save_routine_selenium(url: str, selectors: dict, db: Session) -> int:
    """Selenium 크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    driver = create_selenium_driver()

    current_rates = {}
    try:
        driver.get(url) # url 오류면 여기서 에러남
        wait = WebDriverWait(driver, 10)

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
                logger.warning(f"⚠️ 유효하지 않은 환율: {pair}", extra={"pair": pair, "rate_text": rate_text, "bank": BANK_NAME})
                continue

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
